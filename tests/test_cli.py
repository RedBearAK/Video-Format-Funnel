"""Tests for the command-line entry points: --version, dispatch, the launcher."""

VERSION = "20261102"

import contextlib
import io
import os
import pathlib
import re
import subprocess
import unittest

import vidfunnel
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

    def test_global_options_are_accepted_before_a_command(self):
        parser = cli.build_parser()
        args = parser.parse_args(["--ffmpeg", "/opt/ff/ffmpeg", "--quiet", "check"])
        self.assertEqual(args.ffmpeg, "/opt/ff/ffmpeg")
        self.assertTrue(args.quiet)
        self.assertEqual(args.command, "check")


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
