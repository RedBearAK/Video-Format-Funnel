"""Tests for the command-line entry points: --version, dispatch, the launcher."""

VERSION = "20261107"

import contextlib
import io
import os
import pathlib
import re
import signal
import subprocess
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

    def test_unimplemented_subcommand_says_so(self):
        missing = [entry[0] for entry in cli.SUBCOMMANDS if cli.load_stage(entry[1]) is None]
        if not missing:
            self.skipTest("every stage module exists; no stub left to test")
        name = missing[0]
        code, out, err = run_main([name, "/some/path"])
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertEqual(out, "")
        self.assertIn("'{}' is not implemented yet".format(name), err)

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
        previous = signal.signal(signal.SIGTERM, signal.SIG_DFL)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        self.addCleanup(vidfunnel.interrupt.interrupted.clear)

    def run_with_dispatch(self, dispatch):
        with unittest.mock.patch.object(cli, "dispatch", side_effect=dispatch):
            return run_main(["check"])

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
        self.assertFalse(cli._terminating.is_set())
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)

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

    def test_nothing_is_installed_off_the_main_thread(self):
        result = []
        thread = threading.Thread(target=lambda: result.append(cli.install_sigterm_handler()))
        thread.start()
        thread.join()
        self.assertEqual(result, [None])
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)


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
