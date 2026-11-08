"""Tests for vidfunnel.interrupt: Ctrl+C during plan, convert and refit with --jobs.

The unit tests need only /bin/sh. The worker tests run on fixture copies
from tests/fixtures; the integration tests make a 60 s clip with ffmpeg in
a temporary folder, start 'python3 -m vidfunnel ... --jobs 2' in a session
of its own, send SIGINT to that process group once work has started, and
check what is left on disk. Tests that need ffmpeg or the fixtures skip
with a message when they are missing.
"""

VERSION = "20261107"

import _thread
import glob
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import unittest.mock
from pathlib import Path

import vidfunnel.analyze
import vidfunnel.encode
import vidfunnel.ffmpeg
import vidfunnel.interrupt
import vidfunnel.plan
from vidfunnel.ffmpeg import FfmpegError, ToolNotFound, find_tools, null_decode, run
from vidfunnel.interrupt import Interrupted, interrupted, run_pool
from vidfunnel.record import RECORD_DIR, cheap_fingerprint, fingerprint_matches, load_record, record_path, sha256_file

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(REPO_ROOT, "tests", "fixtures")
SHORT_FIXTURE = "xvid_avi_mp3.avi"
REFIT_SOURCE = "h264_avi_mp3.avi"
COPY_NAMES = ("first.avi", "second.avi", "third.avi", "fourth.avi")
SECONDS_TO_EXIT = 10.0


def _tools_or_none():
    try:
        return find_tools()
    except ToolNotFound:
        return None


TOOLS = _tools_or_none()


def quiet():
    return types.SimpleNamespace(stream=io.StringIO(), quiet=True, verbose=False)


def write_script(path, body):
    with open(str(path), "w") as handle:
        handle.write("#!/bin/sh\n" + body)
    os.chmod(str(path), os.stat(str(path)).st_mode | stat.S_IXUSR)
    return str(path)


class EventTests(unittest.TestCase):
    """vidfunnel.ffmpeg.run, null_decode and the hash loop against the event."""

    def setUp(self):
        interrupted.clear()
        self.addCleanup(interrupted.clear)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.marker = self.folder / "started"
        # Touches a marker so a test can tell whether the program was started at all.
        self.touching = write_script(self.folder / "touching", "touch '{}'\nexit 0\n".format(self.marker))
        # Sleeps a little, then exits as ffmpeg does after it caught SIGINT.
        self.dying = write_script(self.folder / "dying", "sleep 0.5\nexit 255\n")
        self.dying_at_once = write_script(self.folder / "dying_at_once", "exit 255\n")

    def test_run_starts_nothing_while_the_event_is_set(self):
        interrupted.set()
        with self.assertRaises(Interrupted):
            run([self.touching])
        with self.assertRaises(Interrupted):
            run([self.touching], progress=lambda block: None, check=False)
        self.assertFalse(self.marker.exists())
        interrupted.clear()
        run([self.touching])
        self.assertTrue(self.marker.exists())

    def test_null_decode_starts_nothing_while_the_event_is_set(self):
        interrupted.set()
        tools = vidfunnel.ffmpeg.Tools(self.touching, self.touching)
        with self.assertRaises(Interrupted):
            null_decode(tools, self.folder / "input.avi")
        self.assertFalse(self.marker.exists())

    def test_exit_255_while_the_event_is_set_is_an_interruption(self):
        for check in (True, False):
            interrupted.clear()
            timer = threading.Timer(0.1, interrupted.set)
            timer.start()
            self.addCleanup(timer.cancel)
            with self.assertRaises(Interrupted, msg="check={}".format(check)):
                run([self.dying], check=check)

    def test_exit_255_with_the_event_clear_is_unchanged(self):
        with self.assertRaises(FfmpegError) as caught:
            run([self.dying_at_once])
        self.assertEqual(caught.exception.returncode, 255)
        self.assertEqual(run([self.dying_at_once], check=False).returncode, 255)

    def test_worker_waits_for_the_main_thread_to_report_the_interrupt(self):
        """The child's death can be seen before the main thread sets the event."""
        outcome = []

        def worker():
            try:
                run([self.dying_at_once])
                outcome.append("returned")
            except Interrupted:
                outcome.append("interrupted")
            except FfmpegError:
                outcome.append("failed")

        thread = threading.Thread(target=worker)
        thread.start()
        time.sleep(0.3)
        interrupted.set()
        thread.join(10)
        self.assertEqual(outcome, ["interrupted"])

    def test_worker_without_an_interrupt_fails_after_the_grace(self):
        outcome = []

        def worker():
            started = time.monotonic()
            try:
                run([self.dying_at_once])
            except FfmpegError:
                outcome.append(time.monotonic() - started)

        with unittest.mock.patch.object(vidfunnel.interrupt, "GRACE_SECONDS", 0.2):
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(10)
        self.assertEqual(len(outcome), 1)
        self.assertGreaterEqual(outcome[0], 0.2)

    def test_hashing_stops_while_the_event_is_set(self):
        target = self.folder / "data.bin"
        target.write_bytes(b"x" * 1000)
        interrupted.set()
        with self.assertRaises(Interrupted):
            sha256_file(target)
        with self.assertRaises(Interrupted):
            vidfunnel.plan._sha256_paths([target], "data.bin", quiet())

    @unittest.skipIf(shutil.which("par2") is None, "par2 not on PATH; par2 interrupt test skipped")
    def test_par2_starts_nothing_while_the_event_is_set(self):
        output = self.folder / "output.mp4"
        output.write_bytes(b"y" * 4096)
        interrupted.set()
        with self.assertRaises(Interrupted):
            vidfunnel.encode.write_par2(output, 5, quiet())
        self.assertEqual(vidfunnel.encode._par2_files(output), [])


class ChildRegistryTests(unittest.TestCase):
    """vidfunnel.ffmpeg's registry of running children, which the SIGTERM handler signals."""

    def setUp(self):
        interrupted.clear()
        self.addCleanup(interrupted.clear)
        self.sleeper = shutil.which("sleep")
        if self.sleeper is None:
            self.skipTest("'sleep' not on PATH; child registry tests skipped")

    def wait_for_children(self, count):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with vidfunnel.ffmpeg._children_lock:
                if len(vidfunnel.ffmpeg._children) >= count:
                    return
            time.sleep(0.02)
        self.fail("no child was registered within 5 s")

    def stop_sleeping_child(self, progress):
        """Run 'sleep 30' through run() on a worker, then do what the SIGTERM handler does."""
        outcome = []

        def worker():
            try:
                run([self.sleeper, "30"], progress=progress)
            except BaseException as error:  # recorded for the assertions below
                outcome.append(error)

        thread = threading.Thread(target=worker)
        started = time.monotonic()
        thread.start()
        self.wait_for_children(1)
        interrupted.set()
        self.assertEqual(vidfunnel.ffmpeg.terminate_children(), 1)
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], Interrupted)
        with vidfunnel.ffmpeg._children_lock:
            self.assertEqual(vidfunnel.ffmpeg._children, set())

    def test_terminate_children_stops_a_sleeping_child(self):
        self.stop_sleeping_child(None)

    def test_terminate_children_stops_a_sleeping_child_read_for_progress(self):
        self.stop_sleeping_child(lambda block: None)

    def test_terminate_children_stops_a_running_par2(self):
        """encode.write_par2 registers its child: a stand-in par2 that sleeps is stopped by terminate_children."""
        with tempfile.TemporaryDirectory() as temporary:
            fake_bin = Path(temporary) / "bin"
            fake_bin.mkdir()
            write_script(fake_bin / "par2", "exec {} 30\n".format(self.sleeper))
            output = Path(temporary) / "output.mp4"
            output.write_bytes(b"y" * 4096)
            outcome = []

            def worker():
                try:
                    vidfunnel.encode.write_par2(output, 5, quiet())
                except BaseException as error:  # recorded for the assertions below
                    outcome.append(error)

            with unittest.mock.patch.dict(os.environ, {"PATH": "{}:{}".format(fake_bin, os.environ.get("PATH", ""))}):
                thread = threading.Thread(target=worker)
                started = time.monotonic()
                thread.start()
                self.wait_for_children(1)
                interrupted.set()
                self.assertEqual(vidfunnel.ffmpeg.terminate_children(), 1)
                thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(len(outcome), 1)
            self.assertIsInstance(outcome[0], Interrupted)
            with vidfunnel.ffmpeg._children_lock:
                self.assertEqual(vidfunnel.ffmpeg._children, set())

    def test_a_child_registered_after_the_event_was_set_is_terminated(self):
        process = subprocess.Popen([self.sleeper, "30"])
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        interrupted.set()
        vidfunnel.ffmpeg.register_child(process)
        vidfunnel.ffmpeg.forget_child(process)
        self.assertEqual(process.wait(5), -signal.SIGTERM)


class PoolTests(unittest.TestCase):
    """run_pool: Ctrl+C on the main thread sets the event, cancels what has not started and is raised again."""

    def setUp(self):
        interrupted.clear()
        self.addCleanup(interrupted.clear)

    def test_ctrl_c_sets_the_event_and_cancels_queued_tasks(self):
        # interrupt_main acts through Python's SIGINT handler, which a test harness may have replaced.
        previous = signal.signal(signal.SIGINT, signal.default_int_handler)
        self.addCleanup(signal.signal, signal.SIGINT, previous)
        seen = []
        done = []

        def first():
            _thread.interrupt_main()
            seen.append(("first saw the event", interrupted.wait(10)))
            vidfunnel.interrupt.check()
            seen.append("first went on")

        def second():
            seen.append("second ran")

        with self.assertRaises(KeyboardInterrupt):
            run_pool(1, [first, second], lambda index, value: done.append(index))
        self.assertEqual(seen, [("first saw the event", True)])
        self.assertEqual(done, [])
        self.assertFalse(interrupted.is_set(), "the event outlived the pool")

    def test_results_arrive_as_tasks_finish(self):
        gate = threading.Event()
        done = []

        def slow():
            gate.wait(10)
            return "slow"

        def fast():
            return "fast"

        def on_done(index, value):
            done.append(value)
            if value == "fast":
                gate.set()

        run_pool(2, [slow, fast], on_done)
        self.assertEqual(done, ["fast", "slow"])


def _needs(*names):
    missing = [name for name in names if not os.path.exists(os.path.join(FIXTURE_DIR, name))]
    return "fixtures missing ({}); run tests/make_fixtures.sh".format(", ".join(missing)) if missing else None


def _capabilities():
    from vidfunnel.probe import decodable_codecs
    capabilities = dict(vidfunnel.ffmpeg.capabilities(TOOLS))
    capabilities["decodable"] = decodable_codecs(TOOLS)
    return capabilities


def _interrupt_at_part_file(real_run):
    """A stand-in for encode's run_tool: at an argv writing a part file, write some of it, set the event, run."""

    def stand_in(argv, *args, **kwargs):
        if str(argv[-1]).endswith(".part"):
            Path(argv[-1]).write_bytes(b"partial output")
            interrupted.set()
        return real_run(argv, *args, **kwargs)
    return stand_in


@unittest.skipIf(TOOLS is None, "ffmpeg not found on PATH; interrupt worker tests skipped")
class WorkerTests(unittest.TestCase):
    """One item cut off by the interrupt: Interrupted propagates and nothing is written."""

    def setUp(self):
        reason = _needs(SHORT_FIXTURE, REFIT_SOURCE)
        if reason:
            self.skipTest(reason)
        interrupted.clear()
        self.addCleanup(interrupted.clear)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "library"
        self.root.mkdir()

    def copy(self, name):
        target = self.root / name
        shutil.copy2(os.path.join(FIXTURE_DIR, name), str(target))
        return target

    def leftover_parts(self):
        return sorted(name for name in os.listdir(str(self.root)) if name.endswith(".part"))

    def test_plan_item_interrupted_at_the_window_pass_writes_no_record(self):
        source = self.copy(SHORT_FIXTURE)
        real_run = vidfunnel.ffmpeg.run
        passes = []

        def stand_in(argv, *args, **kwargs):
            if "idet" in " ".join(str(part) for part in argv):
                passes.append(argv)
                interrupted.set()
            return real_run(argv, *args, **kwargs)

        work = vidfunnel.plan._Work(source)
        with unittest.mock.patch.object(vidfunnel.analyze, "run", stand_in):
            with self.assertRaises(Interrupted):
                vidfunnel.plan._plan_one(TOOLS, work, vidfunnel.plan.make_options(), [source], _capabilities(),
                                         quiet())
        self.assertTrue(passes, "the window pass was never reached")
        self.assertFalse(record_path(source).exists(), "a record was written for an interrupted item")

    def plan_and_load(self, name, **values):
        source = self.copy(name)
        vidfunnel.plan.plan_paths(TOOLS, [str(self.root)], vidfunnel.plan.make_options(**values), progress=quiet())
        record = load_record(source)
        self.assertEqual(record["status"], "planned")
        return source, record

    def test_convert_item_interrupted_at_the_encode_changes_nothing(self):
        source, record = self.plan_and_load(SHORT_FIXTURE)
        before = record_path(source).read_bytes()
        options = types.SimpleNamespace(par2=None, verbose=False, jobs=1, dry_run=False, refit=False)
        stand_in = _interrupt_at_part_file(vidfunnel.encode.run_tool)
        with unittest.mock.patch.object(vidfunnel.encode, "run_tool", stand_in):
            with self.assertRaises(Interrupted):
                vidfunnel.encode.convert_one(TOOLS, source, record, options, progress=quiet())
        self.assertEqual(record_path(source).read_bytes(), before)
        self.assertEqual(self.leftover_parts(), [])
        self.assertFalse((self.root / (source.stem + ".mp4")).exists())

    def test_refit_item_interrupted_at_the_encode_changes_nothing(self):
        source, _record = self.plan_and_load(REFIT_SOURCE, reencode_video=True)
        options = types.SimpleNamespace(par2=None, verbose=False, jobs=1, dry_run=False, refit=False)
        vidfunnel.encode.convert_paths(TOOLS, [str(self.root)], options, quiet())
        record = load_record(source)
        self.assertIn("output-larger", record["flags"], "the fixture no longer re-encodes larger; pick another")
        output = self.root / record["output"]["name"]
        before = record_path(source).read_bytes()
        output_before = sha256_file(output)
        stand_in = _interrupt_at_part_file(vidfunnel.encode.run_tool)
        with unittest.mock.patch.object(vidfunnel.encode, "run_tool", stand_in):
            with self.assertRaises(Interrupted):
                vidfunnel.encode.refit_one(TOOLS, source, record, options, progress=quiet())
        interrupted.clear()
        self.assertEqual(record_path(source).read_bytes(), before)
        self.assertEqual(sha256_file(output), output_before)
        self.assertEqual(self.leftover_parts(), [])


@unittest.skipIf(TOOLS is None, "ffmpeg not found on PATH; folder lock test skipped")
class FolderLockTests(unittest.TestCase):

    def setUp(self):
        reason = _needs(SHORT_FIXTURE)
        if reason:
            self.skipTest(reason)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "library"
        self.slow = self.root / "slow_folder"
        self.tiny = self.root / "tiny_folder"
        for folder in (self.slow, self.tiny):
            folder.mkdir(parents=True)
            shutil.copy2(os.path.join(FIXTURE_DIR, SHORT_FIXTURE), str(folder / SHORT_FIXTURE))

    def test_a_finished_folder_is_unlocked_while_another_is_still_planned(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        held = threading.Event()
        real_analyze = vidfunnel.plan.analyze

        def stand_in(tools, path, *args, **kwargs):
            if Path(path).parent == self.slow:
                held.set()
                gate.wait(60)
            return real_analyze(tools, path, *args, **kwargs)

        failures = []

        def planning():
            try:
                vidfunnel.plan.plan_paths(TOOLS, [str(self.root)], vidfunnel.plan.make_options(jobs=2),
                                          progress=quiet())
            except BaseException as error:  # reported by the test thread below
                failures.append(error)

        tiny_lock = self.tiny / RECORD_DIR / "lock"
        slow_lock = self.slow / RECORD_DIR / "lock"
        with unittest.mock.patch.object(vidfunnel.plan, "analyze", stand_in):
            thread = threading.Thread(target=planning)
            thread.start()
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if held.is_set() and record_path(self.tiny / SHORT_FIXTURE).exists() and not tiny_lock.exists():
                        break
                    time.sleep(0.05)
                tiny_released = held.is_set() and not tiny_lock.exists()
                slow_still_locked = slow_lock.exists()
                tiny_folder_file = (self.tiny / RECORD_DIR / "folder.json").exists()
            finally:
                gate.set()
                thread.join(120)
        self.assertEqual(failures, [])
        self.assertTrue(tiny_released, "the tiny folder's lock was still held while the slow item waited")
        self.assertTrue(slow_still_locked, "the slow folder's lock was gone while its item was still planned")
        self.assertTrue(tiny_folder_file, "the tiny folder's folder file was not written when its lock went")
        self.assertFalse(slow_lock.exists())
        for folder in (self.slow, self.tiny):
            self.assertEqual(load_record(folder / SHORT_FIXTURE)["status"], "planned")


# --- integration: SIGINT to the process group -------------------------------------------


def _make_long_clip(path):
    """A 60 s MPEG-4 ASP clip with MP3 audio, 720x480: long enough to be interrupted in each stage."""
    subprocess.run([TOOLS.ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=size=720x480:rate=25:duration=60", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=60", "-c:v", "mpeg4", "-q:v", "5", "-c:a", "libmp3lame",
                    "-shortest", str(path)], check=True)


def _interrupt_command(arguments, trigger, settle, signum=signal.SIGINT, group=True, groups=None,
                       again_after=None, environment=None):
    """Run vidfunnel with `arguments`, SIGINT its process group `settle` s after a stdout line holding `trigger`.

    With group=False, `signum` goes to the Python process alone, as 'kill'
    or 'podman stop' sends it, so no child receives it from outside. With
    again_after, the same signal is sent a second time that many seconds
    after the first. The process group id (the child's pid) is appended to
    `groups` when given. `environment` adds variables to the child's.
    Returns (returncode, stdout, stderr, seconds from the first signal to exit).
    """
    environment = dict(os.environ, PYTHONPATH=REPO_ROOT, **(environment or {}))
    process = subprocess.Popen([sys.executable, "-m", "vidfunnel"] + arguments, cwd=REPO_ROOT, env=environment,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, start_new_session=True,
                               preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    if groups is not None:
        groups.append(process.pid)
    lines = []
    try:
        for line in process.stdout:
            lines.append(line)
            if trigger in line:
                break
        else:
            process.wait(30)
            raise AssertionError("'{}' never appeared; output:\n{}".format(trigger, "".join(lines)))
        # The trigger line is printed just before the ffmpeg run it announces starts.
        time.sleep(settle)
        send = (lambda: os.killpg(process.pid, signum)) if group else (lambda: os.kill(process.pid, signum))
        send()
        sent = time.monotonic()
        if again_after is not None:
            time.sleep(again_after)
            try:
                send()
            except ProcessLookupError:
                pass
        rest, errors = process.communicate(timeout=60)
        return process.returncode, "".join(lines) + rest, errors, time.monotonic() - sent
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


@unittest.skipIf(TOOLS is None or not hasattr(os, "killpg"),
                 "ffmpeg not found on PATH (or no process groups); interrupt integration tests skipped")
class InterruptIntegrationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.clip_folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.clip_folder.cleanup)
        cls.clip = Path(cls.clip_folder.name) / "clip.avi"
        _make_long_clip(cls.clip)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "library"
        self.root.mkdir()
        for name in COPY_NAMES:
            shutil.copy2(str(self.clip), str(self.root / name))

    def records(self):
        found = {}
        for path in sorted(glob.glob(str(self.root / RECORD_DIR / "*.avi.json"))):
            with open(path, encoding="utf-8") as handle:
                found[os.path.basename(path)] = json.load(handle)
        return found

    def leftover_parts(self):
        return sorted(name for name in os.listdir(str(self.root)) if name.endswith(".part"))

    def assert_interrupted(self, code, output, errors, seconds):
        self.assertEqual(code, 1, "stdout:\n{}\nstderr:\n{}".format(output, errors))
        self.assertIn("interrupted", errors)
        self.assertLess(seconds, SECONDS_TO_EXIT, "exit came {:.1f} s after SIGINT".format(seconds))
        self.assertEqual(self.leftover_parts(), [])

    def lock_files(self):
        return sorted(glob.glob(str(self.root / "**" / RECORD_DIR / "lock"), recursive=True))

    def slow_window_ffmpeg(self):
        """--ffmpeg arguments for a stand-in that runs the real ffmpeg, except in its idet window pass.

        There it sleeps 30 s, so a run stopped in that pass can only exit in
        time if vidfunnel stops the child itself, and it takes 0.5 s to end
        after a SIGTERM (exit status 143), so a second SIGTERM sent soon
        after the first arrives while the run is still waiting for it.
        Its capability cache goes to a temporary XDG_CACHE_HOME.
        """
        fake_bin = Path(self.temporary.name) / "slow-bin"
        fake_bin.mkdir()
        window = fake_bin / "slow_window.py"
        window.write_text("import signal, sys, time\n"
                          "def stop(signum, frame):\n"
                          "    time.sleep(0.5)\n"
                          "    sys.exit(143)\n"
                          "signal.signal(signal.SIGTERM, stop)\n"
                          "time.sleep(30)\n")
        write_script(fake_bin / "ffmpeg", 'case " $* " in *idet*) exec {} {} ;; esac\nexec {} "$@"\n'.format(
            sys.executable, window, TOOLS.ffmpeg))
        os.symlink(TOOLS.ffprobe, str(fake_bin / "ffprobe"))
        self.extra_environment = {"XDG_CACHE_HOME": str(Path(self.temporary.name) / "cache")}
        return ["--ffmpeg", str(fake_bin / "ffmpeg")]

    def terminate(self, arguments, trigger, settle, again_after=None):
        groups = []

        def kill_leftovers():
            for group_id in groups:
                try:
                    os.killpg(group_id, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        self.addCleanup(kill_leftovers)
        result = _interrupt_command(arguments, trigger, settle, signal.SIGTERM, group=False, groups=groups,
                                    again_after=again_after, environment=getattr(self, "extra_environment", None))
        return result + (groups[0],)

    def assert_terminated(self, code, output, errors, seconds, group_id):
        self.assertEqual(code, 1, "stdout:\n{}\nstderr:\n{}".format(output, errors))
        self.assertIn("vidfunnel: terminated", errors)
        self.assertNotIn("vidfunnel: interrupted", errors)
        self.assertLess(seconds, SECONDS_TO_EXIT, "exit came {:.1f} s after SIGTERM".format(seconds))
        self.assertEqual(self.leftover_parts(), [])
        self.assertEqual(self.lock_files(), [])
        with self.assertRaises(ProcessLookupError, msg="a child of the terminated run is still running"):
            os.killpg(group_id, 0)

    def plan_terminated(self, jobs, again_after=None):
        arguments = self.slow_window_ffmpeg() + ["plan"] + (["--jobs", str(jobs)] if jobs else []) + [str(self.root)]
        code, output, errors, seconds, group_id = self.terminate(arguments, "idet, cropdetect", 0.3,
                                                                 again_after=again_after)
        sys.stderr.write("\n  plan{}{}: exit {:.2f} s after SIGTERM ".format(
            " --jobs {}".format(jobs) if jobs else "", ", SIGTERM twice" if again_after else "", seconds))
        self.assert_terminated(code, output, errors, seconds, group_id)
        records = self.records()
        self.assertLess(len(records), len(COPY_NAMES), "every item finished; nothing was terminated")
        for name, record in records.items():
            self.assertEqual(record["status"], "planned", name)
            self.assertNotIn("decode-failed", record.get("flags") or [], name)

    def test_plan_with_jobs_terminated_by_sigterm(self):
        self.plan_terminated(2)

    def test_plan_with_jobs_terminated_by_two_sigterms(self):
        """The second SIGTERM, 50 ms after the first, does not cut the cleanup short (no 'failed' records)."""
        self.plan_terminated(2, again_after=0.05)

    def test_plan_without_jobs_terminated_by_sigterm(self):
        """The main thread's own ffmpeg child is killed and reaped before the run exits."""
        self.plan_terminated(None)

    def test_convert_with_jobs_terminated_by_sigterm(self):
        self.convert_terminated()

    def test_convert_with_jobs_terminated_by_two_sigterms(self):
        """The second SIGTERM, 50 ms after the first, does not cut the cleanup short (no 'failed' records)."""
        self.convert_terminated(again_after=0.05)

    def convert_terminated(self, again_after=None):
        vidfunnel.plan.plan_paths(TOOLS, [str(self.root)], vidfunnel.plan.make_options(jobs=4), progress=quiet())
        planned = self.records()
        code, output, errors, seconds, group_id = self.terminate(["convert", "--jobs", "2", str(self.root)],
                                                                 "[convert ", 1.0, again_after=again_after)
        sys.stderr.write("\n  convert --jobs 2{}: exit {:.2f} s after SIGTERM ".format(
            ", SIGTERM twice" if again_after else "", seconds))
        self.assert_terminated(code, output, errors, seconds, group_id)
        records = self.records()
        unchanged = 0
        for name, record in records.items():
            if record == planned[name]:
                unchanged += 1
                continue
            self.assertIn(record["status"], ("converted", "verified"), name)
        self.assertGreater(unchanged, 0, "every item finished; nothing was terminated")

    def test_plan_with_jobs_interrupted_in_the_window_pass(self):
        code, output, errors, seconds = _interrupt_command(["plan", "--jobs", "2", str(self.root)],
                                                           "idet, cropdetect", 0.3)
        sys.stderr.write("\n  plan --jobs 2: exit {:.2f} s after SIGINT ".format(seconds))
        self.assert_interrupted(code, output, errors, seconds)
        records = self.records()
        self.assertLess(len(records), len(COPY_NAMES), "every item finished; nothing was interrupted")
        for name, record in records.items():
            self.assertEqual(record["status"], "planned", name)
            self.assertNotIn("decode-failed", record.get("flags") or [], name)
            self.assertNotIn("decode-failed", [entry.get("code") for entry in record.get("penalties") or []], name)
            self.assertTrue(record["stages"]["plan"]["argv"], name)

    def test_convert_with_jobs_interrupted_in_the_encode(self):
        vidfunnel.plan.plan_paths(TOOLS, [str(self.root)], vidfunnel.plan.make_options(jobs=4), progress=quiet())
        planned = self.records()
        self.assertEqual(sorted(record["status"] for record in planned.values()), ["planned"] * len(COPY_NAMES))
        code, output, errors, seconds = _interrupt_command(["convert", "--jobs", "2", str(self.root)],
                                                           "[convert ", 1.0)
        sys.stderr.write("\n  convert --jobs 2: exit {:.2f} s after SIGINT ".format(seconds))
        self.assert_interrupted(code, output, errors, seconds)
        records = self.records()
        self.assertEqual(sorted(records), sorted(planned))
        unchanged = 0
        for name, record in records.items():
            if record == planned[name]:
                unchanged += 1
                continue
            self.assertIn(record["status"], ("converted", "verified"), name)
            output_path = self.root / record["output"]["name"]
            self.assertTrue(fingerprint_matches(record["output"]["fingerprint"], cheap_fingerprint(output_path)),
                            name)
        self.assertGreater(unchanged, 0, "every item finished; nothing was interrupted")

# End of File #
