"""Tests for the command-line entry points: --version, dispatch, the launcher."""

VERSION = "20261109.3"

import contextlib
import errno
import io
import os
import pathlib
import re
import signal
import subprocess
import sys
import threading
import time
import unittest
import unittest.mock

import vidfunnel
import vidfunnel.interrupt
from vidfunnel import cli

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "bin" / "vidfunnel"
DATE_VERSION = re.compile(r"^\d{8}(\.\d+)?$")


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class VersionTests(unittest.TestCase):
    def test_package_version_is_date_based(self):
        self.assertRegex(vidfunnel.VERSION, DATE_VERSION)

    def test_every_module_declares_a_date_version(self):
        versions = cli.module_versions()
        self.assertIn("cli", [name for name, _ in versions])
        for name, version in versions:
            self.assertIsNotNone(version, "module {} has no VERSION".format(name))
            self.assertRegex(version, DATE_VERSION, "module {} VERSION {!r}".format(name, version))

    def test_version_flag_lists_package_and_modules(self):
        code, out, err = run_main(["--version"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(err, "")
        lines = out.splitlines()
        self.assertEqual(lines[0], "vidfunnel " + vidfunnel.VERSION)
        self.assertTrue(any(line.split()[0] == "cli" for line in lines[1:]), out)


class DispatchTests(unittest.TestCase):
    def test_no_command_prints_help_and_exits_usage(self):
        code, out, _err = run_main([])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("usage: vidfunnel", out)

    def test_every_subcommand_is_registered(self):
        parser = cli.build_parser()
        registered = parser._subparsers._group_actions[0].choices
        for entry in cli.SUBCOMMANDS:
            self.assertIn(entry[0], registered)

    def test_ffmpeg_defaults_to_the_environment_variable(self):
        with unittest.mock.patch.dict(os.environ, {cli.FFMPEG_ENV: "/opt/box/ffmpeg"}):
            self.assertEqual(cli.build_parser().parse_args(["check"]).ffmpeg, "/opt/box/ffmpeg")
            self.assertEqual(
                cli.build_parser().parse_args(["--ffmpeg", "/usr/bin/ffmpeg", "check"]).ffmpeg,
                "/usr/bin/ffmpeg", "the option wins over the variable")
        with unittest.mock.patch.dict(os.environ, {cli.FFMPEG_ENV: ""}):
            self.assertIsNone(cli.build_parser().parse_args(["check"]).ffmpeg, "an empty variable means PATH")
        environment = {key: value for key, value in os.environ.items() if key != cli.FFMPEG_ENV}
        with unittest.mock.patch.dict(os.environ, environment, clear=True):
            self.assertIsNone(cli.build_parser().parse_args(["check"]).ffmpeg)

    def test_path_is_optional_for_the_working_stages_and_required_for_retire(self):
        parser = cli.build_parser()
        for name in ("plan", "convert", "verify", "index"):
            self.assertEqual(parser.parse_args([name]).paths, [], name)
            self.assertEqual(parser.parse_args([name, "/a", "/b"]).paths, ["/a", "/b"], name)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["retire"])
        self.assertEqual(raised.exception.code, cli.EXIT_USAGE)
        self.assertIn("PATH", errors.getvalue())
        self.assertIn("never assumes the current directory", parser.parse_args(["retire", "/a"]) and
                      "".join(action.help or "" for action in
                              parser._subparsers._group_actions[0].choices["retire"]._actions))

    def test_paths_or_current_directory(self):
        import argparse
        here = os.getcwd()
        args = argparse.Namespace(paths=[], quiet=False)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.paths_or_current_directory(args, "plan"), [here])
        self.assertEqual(args.paths, [here])
        self.assertEqual(output.getvalue(),
                         "vidfunnel plan: no PATH given; using the current directory '{}'\n".format(here))
        args = argparse.Namespace(paths=[], quiet=True)
        with contextlib.redirect_stdout(io.StringIO()) as silent:
            cli.paths_or_current_directory(args, "verify")
        self.assertEqual((args.paths, silent.getvalue()), ([here], ""))
        args = argparse.Namespace(paths=["/given"], quiet=False)
        with contextlib.redirect_stdout(io.StringIO()) as silent:
            self.assertEqual(cli.paths_or_current_directory(args, "index"), ["/given"])
        self.assertEqual((args.paths, silent.getvalue()), (["/given"], ""))

    def test_global_options_are_accepted_before_a_command(self):
        parser = cli.build_parser()
        args = parser.parse_args(["--ffmpeg", "/opt/ff/ffmpeg", "--quiet", "check"])
        self.assertEqual(args.ffmpeg, "/opt/ff/ffmpeg")
        self.assertTrue(args.quiet)
        self.assertEqual(args.command, "check")


class SigtermTests(unittest.TestCase):
    """main() handles SIGTERM like Ctrl+C while a command runs, and puts the previous handler back."""

    def setUp(self):
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT, signal.SIGUSR1):
            previous = signal.signal(signum, signal.SIG_DFL)
            self.addCleanup(signal.signal, signum, previous)
        self.addCleanup(vidfunnel.interrupt.interrupted.clear)
        self.addCleanup(vidfunnel.interrupt.draining.clear)
        self.addCleanup(vidfunnel.interrupt.drain_tally.reset)

    def run_with_dispatch(self, dispatch):
        with unittest.mock.patch.object(cli, "dispatch", side_effect=dispatch):
            return run_main(["check"])

    def test_sigterm_while_the_handler_is_being_restored_still_returns(self):
        # A SIGTERM landing inside main()'s finally, before the previous handler is back, used to
        # escape main() as an uncaught Terminated.
        real_signal = signal.signal
        fired = []

        def late_sigterm(signum, handler):
            if signum == signal.SIGTERM and handler is not cli._on_sigterm and not fired:
                fired.append(True)
                os.kill(os.getpid(), signal.SIGTERM)
            return real_signal(signum, handler)

        with unittest.mock.patch.object(cli.signal, "signal", side_effect=late_sigterm):
            code, _out, err = self.run_with_dispatch(lambda args: 0)
        self.assertEqual(fired, [True])
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)
        self.assertFalse(cli._terminating)

    def test_handler_is_installed_during_the_command_and_removed_after(self):
        seen = []
        code, _out, err = self.run_with_dispatch(lambda args: seen.append(signal.getsignal(signal.SIGTERM)) or 0)
        self.assertEqual((code, err), (cli.EXIT_OK, ""))
        self.assertEqual(seen, [cli._on_sigterm])
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)

    def test_sigterm_during_a_command_is_reported_as_terminated(self):
        def dispatch(args):
            # Without the handler this SIGTERM would end the test run itself.
            if signal.getsignal(signal.SIGTERM) is not cli._on_sigterm:
                raise AssertionError("main() installed no SIGTERM handler")
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)  # the handler raises Terminated before this ends
            return cli.EXIT_OK

        started = time.monotonic()
        code, _out, err = self.run_with_dispatch(dispatch)
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("vidfunnel: terminated", err)
        self.assertNotIn("interrupted", err)
        self.assertFalse(vidfunnel.interrupt.interrupted.is_set(), "the event outlived the terminated command")
        self.assertFalse(cli._terminating)
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)

    def test_the_handler_takes_no_lock(self):
        """The handler body touches no Event or Flag and calls no terminate_children; it only raises."""
        self.addCleanup(setattr, cli, "_terminating", False)
        refuse = unittest.mock.Mock(side_effect=AssertionError("the SIGTERM handler took a lock"))
        with unittest.mock.patch.object(threading.Event, "set", refuse), \
                unittest.mock.patch.object(threading.Event, "clear", refuse), \
                unittest.mock.patch.object(threading.Event, "wait", refuse), \
                unittest.mock.patch.object(vidfunnel.interrupt.Flag, "set", refuse), \
                unittest.mock.patch.object(vidfunnel.interrupt.Flag, "clear", refuse), \
                unittest.mock.patch("vidfunnel.ffmpeg.terminate_children", refuse):
            with self.assertRaises(cli.Terminated):
                cli._on_sigterm(signal.SIGTERM, None)
        self.assertTrue(cli._terminating)
        refuse.assert_not_called()

    def test_only_the_first_sigterm_raises(self):
        self.addCleanup(setattr, cli, "_terminating", False)
        with self.assertRaises(cli.Terminated):
            cli._on_sigterm(signal.SIGTERM, None)
        self.assertIsNone(cli._on_sigterm(signal.SIGTERM, None))
        self.assertTrue(cli._terminating)

    def test_a_sigterm_after_ctrl_c_still_ends_with_the_message(self):
        """A Terminated raised while a Ctrl+C is being handled is caught, and the steps run again."""
        calls = []

        def stop_workers():
            calls.append("stop")
            if len(calls) == 1:
                raise cli.Terminated()

        def dispatch(args):
            cli._terminating = True
            raise KeyboardInterrupt()

        with unittest.mock.patch("vidfunnel.interrupt.stop_workers", side_effect=stop_workers):
            code, _out, err = self.run_with_dispatch(dispatch)
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertEqual(calls, ["stop", "stop"])
        self.assertEqual(err.count("vidfunnel: terminated"), 1, err)

    def test_sigterm_pairs_never_hang(self):
        """SIGTERM pairs 0 to 300 microseconds apart, while the main thread loops on clear() and set()."""
        child_code = (
            "import sys\n"
            "from vidfunnel import cli\n"
            "from vidfunnel.interrupt import interrupted\n"
            "def busy(args):\n"
            "    sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
            "    while True:\n"
            "        interrupted.clear(); interrupted.set()\n"
            "cli.dispatch = busy\n"
            "sys.exit(cli.main(['check']))\n"
        )
        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
        trials = 300
        failures = []
        for trial in range(trials):
            gap = (trial % 31) * 10e-6
            child = subprocess.Popen([sys.executable, "-c", child_code], stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=env, text=True)
            try:
                self.assertEqual(child.stdout.readline(), "ready\n")
                os.kill(child.pid, signal.SIGTERM)
                until = time.perf_counter() + gap
                while time.perf_counter() < until:
                    pass
                os.kill(child.pid, signal.SIGTERM)
                try:
                    _out, err = child.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate()
                    failures.append("trial {}: gap {:.0f} us: no exit within 5 s".format(trial, gap * 1e6))
                    continue
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
                child.stdout.close()
                child.stderr.close()
            # A second SIGTERM that lands after main() put SIG_DFL back ends the process by the signal.
            if "vidfunnel: terminated" not in err or child.returncode not in (cli.EXIT_ERROR, -signal.SIGTERM):
                failures.append("trial {}: gap {:.0f} us: status {}, stderr {!r}".format(
                    trial, gap * 1e6, child.returncode, err[-300:]))
        self.assertEqual(failures, [], "{} of {} trials failed".format(len(failures), trials))

    def test_ctrl_c_is_still_reported_as_interrupted(self):
        def dispatch(args):
            raise KeyboardInterrupt()

        code, _out, err = self.run_with_dispatch(dispatch)
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("vidfunnel: interrupted", err)

    def test_an_existing_handler_is_left_alone(self):
        def own_handler(signum, frame):
            pass

        signal.signal(signal.SIGTERM, own_handler)
        seen = []
        self.run_with_dispatch(lambda args: seen.append(signal.getsignal(signal.SIGTERM)) or 0)
        self.assertEqual(seen, [own_handler])
        self.assertIs(signal.getsignal(signal.SIGTERM), own_handler)

    def stopped_by(self, signum):
        def dispatch(args):
            os.kill(os.getpid(), signum)
            time.sleep(5)  # the handler raises Terminated before this ends
            return cli.EXIT_OK

        started = time.monotonic()
        code, _out, err = self.run_with_dispatch(dispatch)
        self.assertLess(time.monotonic() - started, 4)
        return code, err

    def test_sigquit_and_sighup_stop_the_command_like_sigterm_and_are_named(self):
        for signum, name in ((signal.SIGQUIT, "SIGQUIT"), (signal.SIGHUP, "SIGHUP"), (signal.SIGTERM, "SIGTERM")):
            with self.subTest(signal=name):
                code, err = self.stopped_by(signum)
                self.assertEqual(code, cli.EXIT_ERROR)
                self.assertIn("vidfunnel: terminated ({})".format(name), err)
                self.assertFalse(cli._terminating)
                self.assertIsNone(cli._stop_signal)
                for restored in (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT, signal.SIGUSR1):
                    self.assertIs(signal.getsignal(restored), signal.SIG_DFL)

    def test_a_sighup_ignored_as_nohup_does_is_left_alone(self):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        seen = []
        self.run_with_dispatch(lambda args: seen.append((signal.getsignal(signal.SIGHUP),
                                                         signal.getsignal(signal.SIGQUIT))) or 0)
        self.assertEqual(seen, [(signal.SIG_IGN, cli._on_sigterm)])
        self.assertIs(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)
        self.assertIs(signal.getsignal(signal.SIGQUIT), signal.SIG_DFL)

    def test_a_hangup_whose_stderr_is_gone_still_cleans_up(self):
        """After a hangup, writing the last line fails (EIO on a closed terminal); main() still returns 1 and restores."""
        broken = unittest.mock.Mock()
        broken.write.side_effect = OSError(errno.EIO, "Input/output error")
        broken.fileno.side_effect = OSError(errno.EBADF, "no descriptor")

        def dispatch(args):
            sys.stderr = broken
            os.kill(os.getpid(), signal.SIGHUP)
            time.sleep(5)
            return cli.EXIT_OK

        with unittest.mock.patch.object(sys, "stderr", sys.stderr):
            with unittest.mock.patch.object(cli, "dispatch", side_effect=dispatch):
                code = cli.main(["check"])
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertTrue(broken.write.called, "the final line was attempted")
        self.assertFalse(cli._terminating)
        self.assertIs(signal.getsignal(signal.SIGHUP), signal.SIG_DFL)

    def test_a_closed_stderr_does_not_cut_the_cleanup_short(self):
        closed = io.StringIO()
        closed.close()

        def dispatch(args):
            sys.stderr = closed
            os.kill(os.getpid(), signal.SIGHUP)
            time.sleep(5)
            return cli.EXIT_OK

        with unittest.mock.patch.object(sys, "stderr", sys.stderr):
            with unittest.mock.patch.object(cli, "dispatch", side_effect=dispatch):
                code = cli.main(["check"])
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertFalse(cli._terminating)
        self.assertIs(signal.getsignal(signal.SIGHUP), signal.SIG_DFL)

    def test_a_pipe_whose_reader_is_gone_is_pointed_at_dev_null(self):
        """EPIPE on the final line (tee killed with the run): the descriptor goes to /dev/null, so the flush at exit
        cannot fail either."""
        reader, writer = os.pipe()
        os.close(reader)
        stream = open(writer, "w", buffering=1)
        self.addCleanup(stream.close)

        def dispatch(args):
            sys.stderr = stream
            os.kill(os.getpid(), signal.SIGQUIT)
            time.sleep(5)
            return cli.EXIT_OK

        with unittest.mock.patch.object(sys, "stderr", sys.stderr):
            with unittest.mock.patch.object(cli, "dispatch", side_effect=dispatch):
                code = cli.main(["check"])
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertEqual(os.path.realpath("/proc/self/fd/{}".format(writer)) if os.path.exists("/proc/self/fd")
                         else os.devnull, os.devnull)
        stream.write("later line\n")
        stream.flush()

    def capture_fd2(self):
        """Point file descriptor 2 at a temporary file until cleanup; returns a function reading it."""
        import tempfile
        holder = tempfile.TemporaryFile()
        saved = os.dup(2)
        os.dup2(holder.fileno(), 2)

        def restore():
            os.dup2(saved, 2)
            os.close(saved)
            holder.close()

        self.addCleanup(restore)

        def read():
            holder.seek(0)
            return holder.read().decode("utf-8")

        return read

    def test_sigusr1_starts_a_drain_and_says_so_once(self):
        read = self.capture_fd2()
        self.assertIsNone(cli._on_sigusr1(signal.SIGUSR1, None))
        self.assertTrue(vidfunnel.interrupt.draining.is_set())
        self.assertFalse(vidfunnel.interrupt.interrupted.is_set())
        cli._on_sigusr1(signal.SIGUSR1, None)
        self.assertEqual(read(), cli.DRAIN_NOTICE)
        self.assertIn("finishing the items in progress, starting no more", read())

    def test_the_drain_handler_takes_no_lock(self):
        self.capture_fd2()
        refuse = unittest.mock.Mock(side_effect=AssertionError("the SIGUSR1 handler took a lock"))
        with unittest.mock.patch.object(threading.Event, "set", refuse), \
                unittest.mock.patch.object(threading.Event, "wait", refuse), \
                unittest.mock.patch.object(sys, "stderr", unittest.mock.Mock(write=refuse, flush=refuse)), \
                unittest.mock.patch("vidfunnel.ffmpeg.terminate_children", refuse):
            cli._on_sigusr1(signal.SIGUSR1, None)
        refuse.assert_not_called()

    def test_a_drained_command_keeps_its_exit_code_and_reports_the_counts(self):
        read = self.capture_fd2()

        def dispatch(args):
            os.kill(os.getpid(), signal.SIGUSR1)
            self.assertTrue(vidfunnel.interrupt.draining.is_set())
            vidfunnel.interrupt.drain_after_item()
            self.assertTrue(vidfunnel.interrupt.drain_before_next(3))
            return 0

        with unittest.mock.patch.object(cli, "dispatch", side_effect=dispatch):
            code, _out, err = run_main(["convert"])
        self.assertEqual(code, 0)
        self.assertIn("vidfunnel: drained: finished 1 in progress, started no more (3 left planned)", err)
        self.assertIn(cli.DRAIN_NOTICE, read())
        self.assertFalse(vidfunnel.interrupt.draining.is_set(), "the drain outlived the command")
        self.assertIs(signal.getsignal(signal.SIGUSR1), signal.SIG_DFL)

    def test_a_command_that_does_not_drain_says_it_ran_to_its_end(self):
        self.capture_fd2()

        def dispatch(args):
            os.kill(os.getpid(), signal.SIGUSR1)
            return 0

        code, _out, err = self.run_with_dispatch(dispatch)
        self.assertEqual(code, 0)
        self.assertIn("'check': it has no items to stop between; it ran to its end", err)

    def test_sigterm_during_a_drain_stops_now(self):
        self.capture_fd2()

        def dispatch(args):
            os.kill(os.getpid(), signal.SIGUSR1)
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)
            return 0

        code, _out, err = self.run_with_dispatch(dispatch)
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("vidfunnel: terminated (SIGTERM)", err)
        self.assertNotIn("drained", err)
        self.assertFalse(vidfunnel.interrupt.draining.is_set())

    def test_nothing_is_installed_off_the_main_thread(self):
        result = []
        thread = threading.Thread(target=lambda: result.append(cli.install_sigterm_handler()))
        thread.start()
        thread.join()
        self.assertEqual(result, [None])
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT, signal.SIGUSR1):
            self.assertIs(signal.getsignal(signum), signal.SIG_DFL)


class LauncherTests(unittest.TestCase):
    def test_launcher_runs_package_without_pythonpath_in_environment(self):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        result = subprocess.run(
            [str(LAUNCHER), "--version"],
            capture_output=True, text=True, env=env, cwd="/",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("vidfunnel " + vidfunnel.VERSION), result.stdout)

    def test_launcher_follows_a_relative_symlink(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            # macOS keeps temp dirs under /var, a symlink to /private/var; the
            # relative path must be computed from the resolved location or
            # its '..' steps come out one short.
            tmp = os.path.realpath(tmp)
            link = pathlib.Path(tmp) / "vidfunnel"
            link.symlink_to(os.path.relpath(LAUNCHER, tmp))
            result = subprocess.run([str(link), "--version"], capture_output=True, text=True, cwd="/")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.startswith("vidfunnel "), result.stdout)


if __name__ == "__main__":
    unittest.main()

# End of File #
