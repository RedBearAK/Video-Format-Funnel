"""Tests for bin/vidfunnel-podman, run with sh against a fake container engine.

A fake 'podman' first on PATH writes its arguments one per line to a log file
and exits 0, so no container is started. HOME is a temporary folder in every
test; nothing here reads or writes the real ~/.local or any rc file.
"""

VERSION = "20261107"

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / "bin" / "vidfunnel-podman"
PLAIN_LAUNCHER = REPO_ROOT / "bin" / "vidfunnel"
SHELL_BINARY = shutil.which("sh")

FAKE_ENGINE = """#!/bin/sh
for argument in "$@"; do printf '%s\\n' "$argument"; done > "$FAKE_ENGINE_LOG"
exit 0
"""

FAKE_OLD_PYTHON = """#!/bin/sh
exit 1
"""


@unittest.skipIf(SHELL_BINARY is None, "sh is not on PATH; the wrapper cannot be run")
class WrapperTestCase(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix="vidfunnel-podman-")
        self.addCleanup(workspace.cleanup)
        base = Path(workspace.name).resolve()
        self.home = base / "home"
        self.home.mkdir()
        self.workdir = base / "work"
        self.workdir.mkdir()
        self.fake_bin = base / "fake-bin"
        self.fake_bin.mkdir()
        self.engine_log = base / "engine-argv.txt"
        self.write_script("podman", FAKE_ENGINE)
        # The host python the wrapper finds is the one running these tests.
        os.symlink(sys.executable, str(self.fake_bin / "python3"))
        self.user_link = self.home / ".local" / "bin" / "vidfunnel"
        self.state_dir = self.home / ".local" / "state" / "vidfunnel"
        self.cache_dir = self.home / ".cache" / "vidfunnel"

    def write_script(self, name, text):
        path = self.fake_bin / name
        if os.path.lexists(str(path)):
            os.unlink(str(path))
        path.write_text(text)
        os.chmod(str(path), 0o755)

    def use_old_python(self):
        self.write_script("python3", FAKE_OLD_PYTHON)

    def environment(self, home=True, shell="/bin/sh"):
        env = {
            "PATH": "{}:/usr/bin:/bin".format(self.fake_bin),
            "FAKE_ENGINE_LOG": str(self.engine_log),
            "SHELL": shell,
            "LC_ALL": "C",
        }
        if home:
            env["HOME"] = str(self.home)
        return env

    def run_wrapper(self, *arguments, **options):
        result = subprocess.run(
            [SHELL_BINARY, str(WRAPPER)] + list(arguments), cwd=str(self.workdir),
            env=self.environment(**options), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        return result.returncode, result.stdout + result.stderr

    def engine_argv(self):
        return self.engine_log.read_text().splitlines()

    def assert_pair(self, argv, first, second):
        pairs = list(zip(argv, argv[1:]))
        self.assertIn((first, second), pairs, "argv was: {}".format(argv))


class HomeTests(WrapperTestCase):
    def test_container_gets_home_and_its_vidfunnel_folders(self):
        code, output = self.run_wrapper("check")
        self.assertEqual(code, 0, output)
        argv = self.engine_argv()
        self.assertEqual(argv[:2], ["run", "--rm"])
        self.assert_pair(argv, "-e", "HOME={}".format(self.home))
        self.assert_pair(argv, "-v", "{0}:{0}".format(self.state_dir))
        self.assert_pair(argv, "-v", "{0}:{0}".format(self.cache_dir))
        self.assert_pair(argv, "--workdir", str(self.workdir))
        self.assertEqual(argv[-1], "check")
        self.assertTrue(self.state_dir.is_dir())
        self.assertTrue(self.cache_dir.is_dir())
        self.assertEqual(sorted(path.name for path in self.workdir.iterdir()), [])

    def test_home_under_the_working_directory_is_not_mounted_twice(self):
        self.home = self.workdir
        self.state_dir = self.home / ".local" / "state" / "vidfunnel"
        code, output = self.run_wrapper("check")
        self.assertEqual(code, 0, output)
        argv = self.engine_argv()
        self.assertEqual(argv.count("-v"), 1, argv)
        self.assert_pair(argv, "-v", "{0}:{0}".format(self.workdir))
        self.assertTrue(self.state_dir.is_dir())

    def test_unset_home_stops_before_the_engine(self):
        code, output = self.run_wrapper("check", home=False)
        self.assertNotEqual(code, 0)
        self.assertIn("HOME is not set (or is empty)", output)
        self.assertFalse(self.engine_log.exists())

    def test_empty_home_stops_before_the_engine(self):
        self.home = ""
        code, output = self.run_wrapper("check")
        self.assertNotEqual(code, 0)
        self.assertIn("HOME is not set (or is empty)", output)
        self.assertFalse(self.engine_log.exists())

    def test_relative_home_stops_before_the_engine(self):
        self.home = "relative-home"
        code, output = self.run_wrapper("check")
        self.assertNotEqual(code, 0)
        self.assertIn("HOME is 'relative-home', not an absolute path", output)
        self.assertFalse(self.engine_log.exists())
        self.assertFalse((self.workdir / "relative-home").exists())

    def test_uncreatable_state_folder_is_named(self):
        (self.home / ".local").write_text("a file where a folder should be\n")
        code, output = self.run_wrapper("check")
        self.assertNotEqual(code, 0)
        self.assertIn("'{}'".format(self.state_dir), output)
        self.assertFalse(self.engine_log.exists())


class InstallWithHostPythonTests(WrapperTestCase):
    def test_install_and_uninstall_link_to_the_wrapper_without_the_engine(self):
        code, output = self.run_wrapper("--install-command", "--yes")
        self.assertEqual(code, 0, output)
        self.assertFalse(self.engine_log.exists())
        self.assertTrue(self.user_link.is_symlink())
        self.assertEqual(os.readlink(str(self.user_link)), str(WRAPPER))
        code, output = self.run_wrapper("--uninstall-command")
        self.assertEqual(code, 0, output)
        self.assertFalse(os.path.lexists(str(self.user_link)))
        self.assertFalse(self.engine_log.exists())

    def test_install_after_a_leading_option(self):
        code, output = self.run_wrapper("--yes", "--install-command")
        self.assertEqual(code, 0, output)
        self.assertFalse(self.engine_log.exists())
        self.assertEqual(os.readlink(str(self.user_link)), str(WRAPPER))

    def test_link_to_the_plain_launcher_is_replaced(self):
        self.user_link.parent.mkdir(parents=True)
        os.symlink(str(PLAIN_LAUNCHER), str(self.user_link))
        code, output = self.run_wrapper("--install-command", "--yes")
        self.assertEqual(code, 0, output)
        self.assertEqual(os.readlink(str(self.user_link)), str(WRAPPER))
        self.assertIn("other launcher", output)


class InstallWithoutHostPythonTests(WrapperTestCase):
    def setUp(self):
        super().setUp()
        self.use_old_python()

    def test_sh_install_and_uninstall(self):
        code, output = self.run_wrapper("--install-command", "--yes", shell="/bin/bash")
        self.assertEqual(code, 0, output)
        self.assertFalse(self.engine_log.exists())
        self.assertIn("wrapper's own installer", output)
        self.assertEqual(os.readlink(str(self.user_link)), str(WRAPPER))
        self.assertIn("'{}' is not on PATH.".format(self.user_link.parent), output)
        self.assertIn('export PATH="$HOME/.local/bin:$PATH"', output)
        self.assertIn("'~/.bashrc'", output)
        self.assertFalse((self.home / ".bashrc").exists())
        code, output = self.run_wrapper("--uninstall-command")
        self.assertEqual(code, 0, output)
        self.assertFalse(os.path.lexists(str(self.user_link)))
        self.assertFalse(self.engine_log.exists())

    def test_sh_install_names_the_fish_line(self):
        code, output = self.run_wrapper("--install-command", shell="/usr/bin/fish")
        self.assertEqual(code, 0, output)
        self.assertIn("fish_add_path -g $HOME/.local/bin", output)
        self.assertIn("'~/.config/fish/config.fish'", output)

    def test_sh_install_prints_no_path_line_when_on_path(self):
        env_path_dir = self.user_link.parent
        env_path_dir.mkdir(parents=True)
        result = subprocess.run(
            [SHELL_BINARY, str(WRAPPER), "--install-command"], cwd=str(self.workdir),
            env=dict(self.environment(), PATH="{}:{}:/usr/bin:/bin".format(
                self.fake_bin, env_path_dir)),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("export PATH", result.stdout)

    def test_sh_install_replaces_link_to_the_plain_launcher(self):
        self.user_link.parent.mkdir(parents=True)
        os.symlink(str(PLAIN_LAUNCHER), str(self.user_link))
        code, output = self.run_wrapper("--install-command")
        self.assertEqual(code, 0, output)
        self.assertEqual(os.readlink(str(self.user_link)), str(WRAPPER))

    def test_sh_install_refuses_a_foreign_file(self):
        self.user_link.parent.mkdir(parents=True)
        self.user_link.write_text("someone else's script\n")
        code, output = self.run_wrapper("--install-command")
        self.assertNotEqual(code, 0)
        self.assertIn("'{}'".format(self.user_link), output)
        self.assertIn("not a symlink", output)
        self.assertEqual(self.user_link.read_text(), "someone else's script\n")

    def test_sh_install_refuses_a_foreign_symlink(self):
        self.user_link.parent.mkdir(parents=True)
        other_program = self.home / "other-program"
        other_program.write_text("#!/bin/sh\n")
        os.symlink(str(other_program), str(self.user_link))
        code, output = self.run_wrapper("--install-command")
        self.assertNotEqual(code, 0)
        self.assertIn("'{}'".format(other_program), output)
        self.assertEqual(os.readlink(str(self.user_link)), str(other_program))

    def test_sh_uninstall_leaves_a_foreign_symlink_alone(self):
        self.user_link.parent.mkdir(parents=True)
        other_program = self.home / "other-program"
        other_program.write_text("#!/bin/sh\n")
        os.symlink(str(other_program), str(self.user_link))
        code, output = self.run_wrapper("--uninstall-command")
        self.assertNotEqual(code, 0)
        self.assertIn("leaving it alone", output)
        self.assertEqual(os.readlink(str(self.user_link)), str(other_program))

    def test_sh_system_install_without_write_access_prints_the_sudo_command(self):
        if os.access("/usr/local/bin", os.W_OK):
            self.skipTest("'/usr/local/bin' is writable here; the refusal cannot be tested")
        code, output = self.run_wrapper("--install-command", "--system")
        self.assertNotEqual(code, 0)
        self.assertIn("sudo ln -s '{}' '/usr/local/bin/vidfunnel'".format(WRAPPER), output)
        self.assertFalse(self.engine_log.exists())

    def test_sh_install_rejects_unknown_options(self):
        code, output = self.run_wrapper("--install-command", "--launcher", "/elsewhere")
        self.assertNotEqual(code, 0)
        self.assertIn("'--launcher'", output)
        self.assertFalse(os.path.lexists(str(self.user_link)))


if __name__ == "__main__":
    unittest.main()

# End of File #
