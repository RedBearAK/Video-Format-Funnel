"""Tests for vidfunnel.installer: --install-command and --uninstall-command.

Every test works on a temporary HOME and temporary bin directories; nothing
here reads or writes the real ~/.local/bin, /usr/local/bin or any real rc file.
"""

VERSION = "20261107"

import argparse
import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from vidfunnel import installer

POSIX_LINE = 'export PATH="$HOME/.local/bin:$PATH"'
FISH_LINE = "fish_add_path -g $HOME/.local/bin"
SYSTEM_PATH = "/usr/bin:/bin"


class InstallerTestCase(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="vidfunnel-installer-")
        self.addCleanup(self.workspace.cleanup)
        self.home = Path(self.workspace.name) / "home"
        self.home.mkdir()
        self.user_bin = self.home / ".local" / "bin"
        self.target = self.user_bin / "vidfunnel"
        self.launcher = installer.launcher_path()
        self.sudo_calls = []

    def fake_runner(self, command):
        self.sudo_calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    def do_install(self, shell="/bin/bash", path_env=SYSTEM_PATH, yes=True, answer="",
                   **extra):
        output = io.StringIO()
        code = installer.install(
            scope=extra.pop("scope", "user"), yes=yes, home=self.home,
            stdin=io.StringIO(answer), stdout=output,
            environ={"PATH": path_env, "SHELL": shell}, runner=self.fake_runner, **extra)
        return code, output.getvalue()

    def do_uninstall(self, **extra):
        output = io.StringIO()
        code = installer.uninstall(scope=extra.pop("scope", "user"), home=self.home,
                                   stdout=output, runner=self.fake_runner, **extra)
        return code, output.getvalue()


class PathTests(InstallerTestCase):
    def test_launcher_is_the_repo_bin_vidfunnel(self):
        repo_root = Path(__file__).resolve().parent.parent
        self.assertEqual(self.launcher, repo_root / "bin" / "vidfunnel")
        self.assertTrue(self.launcher.is_file())

    def test_target_paths(self):
        self.assertEqual(installer.target_path("user", self.home), self.target)
        self.assertEqual(installer.target_path("system"), Path("/usr/local/bin/vidfunnel"))
        with self.assertRaises(ValueError):
            installer.target_path("everywhere")

    def test_path_has(self):
        directory = str(self.user_bin)
        self.assertTrue(installer.path_has(directory, "/usr/bin:" + directory))
        self.assertTrue(installer.path_has(directory, directory + "/:/usr/bin"))
        self.assertFalse(installer.path_has(directory, "/usr/bin:/bin"))
        self.assertFalse(installer.path_has(directory, ""))
        self.assertFalse(installer.path_has(directory, directory + "/extra"))

    def test_rc_file_for_shell(self):
        self.assertEqual(installer.rc_file_for_shell("/bin/bash", self.home),
                         (self.home / ".bashrc", POSIX_LINE))
        self.assertEqual(installer.rc_file_for_shell("/usr/bin/zsh", self.home),
                         (self.home / ".zprofile", POSIX_LINE))
        self.assertEqual(installer.rc_file_for_shell("/usr/bin/fish", self.home),
                         (self.home / ".config" / "fish" / "config.fish", FISH_LINE))
        self.assertIsNone(installer.rc_file_for_shell("/bin/tcsh", self.home))


class InstallTests(InstallerTestCase):
    def test_fresh_install_creates_symlink_to_launcher(self):
        code, output = self.do_install(path_env=str(self.user_bin))
        self.assertEqual(code, 0)
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(os.readlink(str(self.target)), str(self.launcher))
        self.assertIn("Installed '{}'".format(self.target), output)

    def test_second_install_is_a_no_op(self):
        self.do_install(path_env=str(self.user_bin))
        before = os.lstat(str(self.target))
        code, output = self.do_install(path_env=str(self.user_bin))
        after = os.lstat(str(self.target))
        self.assertEqual(code, 0)
        self.assertEqual(before.st_ino, after.st_ino)
        self.assertIn("already installed", output)

    def test_regular_file_at_target_is_refused(self):
        self.user_bin.mkdir(parents=True)
        self.target.write_text("someone else's script\n")
        code, output = self.do_install()
        self.assertEqual(code, 1)
        self.assertIn("'{}'".format(self.target), output)
        self.assertIn("not a symlink", output)
        self.assertEqual(self.target.read_text(), "someone else's script\n")

    def test_foreign_symlink_at_target_is_refused(self):
        self.user_bin.mkdir(parents=True)
        other_program = Path(self.workspace.name) / "other-program"
        other_program.write_text("#!/bin/sh\n")
        os.symlink(str(other_program), str(self.target))
        code, output = self.do_install()
        self.assertEqual(code, 1)
        self.assertIn("'{}'".format(other_program), output)
        self.assertEqual(os.readlink(str(self.target)), str(other_program))

    def test_dangling_symlink_to_old_repo_is_replaced(self):
        self.user_bin.mkdir(parents=True)
        old_launcher = Path(self.workspace.name) / "moved-away-repo" / "bin" / "vidfunnel"
        os.symlink(str(old_launcher), str(self.target))
        code, output = self.do_install(path_env=str(self.user_bin))
        self.assertEqual(code, 0)
        self.assertEqual(os.readlink(str(self.target)), str(self.launcher))
        self.assertIn("'{}'".format(old_launcher), output)

    def test_dangling_symlink_to_something_else_is_refused(self):
        self.user_bin.mkdir(parents=True)
        missing_program = Path(self.workspace.name) / "gone" / "other-program"
        os.symlink(str(missing_program), str(self.target))
        code, _output = self.do_install()
        self.assertEqual(code, 1)
        self.assertEqual(os.readlink(str(self.target)), str(missing_program))

    def test_install_into_given_bin_dir_does_not_offer_rc_line(self):
        other_bin = Path(self.workspace.name) / "other-bin"
        other_bin.mkdir()
        code, output = self.do_install(bin_dir=other_bin)
        self.assertEqual(code, 0)
        self.assertEqual(os.readlink(str(other_bin / "vidfunnel")), str(self.launcher))
        self.assertNotIn("Append", output)
        self.assertFalse((self.home / ".bashrc").exists())


class LauncherOptionTests(InstallerTestCase):
    def setUp(self):
        super().setUp()
        self.wrapper = installer.podman_wrapper_path()

    def test_wrapper_path_is_next_to_the_launcher(self):
        self.assertEqual(self.wrapper, self.launcher.parent / "vidfunnel-podman")
        self.assertTrue(self.wrapper.is_file())

    def test_install_links_to_the_given_launcher(self):
        code, output = self.do_install(path_env=str(self.user_bin), launcher=self.wrapper)
        self.assertEqual(code, 0, output)
        self.assertEqual(os.readlink(str(self.target)), str(self.wrapper))
        code, output = self.do_install(path_env=str(self.user_bin), launcher=self.wrapper)
        self.assertEqual(code, 0, output)
        self.assertIn("already installed", output)

    def test_missing_given_launcher_is_refused(self):
        missing = Path(self.workspace.name) / "no-such-launcher"
        code, output = self.do_install(launcher=missing)
        self.assertEqual(code, 1)
        self.assertIn("'{}'".format(missing), output)
        self.assertFalse(os.path.lexists(str(self.target)))

    def test_link_to_plain_launcher_is_replaced_when_installing_the_wrapper(self):
        self.user_bin.mkdir(parents=True)
        os.symlink(str(self.launcher), str(self.target))
        code, output = self.do_install(path_env=str(self.user_bin), launcher=self.wrapper)
        self.assertEqual(code, 0, output)
        self.assertEqual(os.readlink(str(self.target)), str(self.wrapper))
        self.assertIn("other launcher", output)

    def test_link_to_wrapper_is_replaced_when_installing_the_plain_launcher(self):
        self.user_bin.mkdir(parents=True)
        os.symlink(str(self.wrapper), str(self.target))
        code, output = self.do_install(path_env=str(self.user_bin))
        self.assertEqual(code, 0, output)
        self.assertEqual(os.readlink(str(self.target)), str(self.launcher))
        self.assertIn("other launcher", output)

    def test_dangling_link_to_an_old_wrapper_is_replaced(self):
        self.user_bin.mkdir(parents=True)
        old_wrapper = Path(self.workspace.name) / "moved-away-repo" / "bin" / "vidfunnel-podman"
        os.symlink(str(old_wrapper), str(self.target))
        code, output = self.do_install(path_env=str(self.user_bin), launcher=self.wrapper)
        self.assertEqual(code, 0, output)
        self.assertEqual(os.readlink(str(self.target)), str(self.wrapper))
        self.assertIn("'{}'".format(old_wrapper), output)

    def test_uninstall_removes_only_a_link_to_the_given_launcher(self):
        self.user_bin.mkdir(parents=True)
        os.symlink(str(self.launcher), str(self.target))
        code, output = self.do_uninstall(launcher=self.wrapper)
        self.assertEqual(code, 1)
        self.assertIn("leaving it alone", output)
        self.assertEqual(os.readlink(str(self.target)), str(self.launcher))
        os.unlink(str(self.target))
        os.symlink(str(self.wrapper), str(self.target))
        code, output = self.do_uninstall()
        self.assertEqual(code, 1)
        self.assertEqual(os.readlink(str(self.target)), str(self.wrapper))
        code, output = self.do_uninstall(launcher=self.wrapper)
        self.assertEqual(code, 0, output)
        self.assertFalse(os.path.lexists(str(self.target)))

    def test_run_passes_the_launcher_through(self):
        parser = argparse.ArgumentParser()
        installer.add_arguments(parser)
        parser.add_argument("--yes", action="store_true")
        args = parser.parse_args(["--install-command", "--yes", "--launcher", str(self.wrapper)])
        calls = []
        original = installer.install
        installer.install = lambda **keywords: calls.append(keywords) or 0
        try:
            self.assertEqual(installer.run(args), 0)
        finally:
            installer.install = original
        self.assertEqual(calls, [{"scope": "user", "yes": True, "launcher": str(self.wrapper)}])


class SystemScopeTests(InstallerTestCase):
    def test_linux_unwritable_directory_uses_sudo_for_the_symlink_only(self):
        locked_bin = Path(self.workspace.name) / "locked-bin"
        locked_bin.mkdir()
        os.chmod(str(locked_bin), 0o555)
        self.addCleanup(os.chmod, str(locked_bin), 0o755)
        if os.access(str(locked_bin), os.W_OK):
            self.skipTest("running as root: a 0555 directory is still writable")
        code, output = self.do_install(scope="system", bin_dir=locked_bin,
                                       platform_name="Linux")
        target = locked_bin / "vidfunnel"
        self.assertEqual(self.sudo_calls, [
            ["sudo", "mkdir", "-p", str(locked_bin)],
            ["sudo", "ln", "-sfn", str(self.launcher), str(target)],
        ])
        self.assertIn("running: sudo ln -sfn", output)
        # The fake runner did nothing, so the check after it must report failure.
        self.assertEqual(code, 1)

    def test_writable_system_directory_needs_no_sudo(self):
        open_bin = Path(self.workspace.name) / "open-bin"
        open_bin.mkdir()
        code, _output = self.do_install(scope="system", bin_dir=open_bin,
                                        platform_name="Linux")
        self.assertEqual(code, 0)
        self.assertEqual(self.sudo_calls, [])
        self.assertEqual(os.readlink(str(open_bin / "vidfunnel")), str(self.launcher))


class RcLineTests(InstallerTestCase):
    def check_shell(self, shell, rc_path, line):
        code, output = self.do_install(shell=shell, yes=True)
        self.assertEqual(code, 0)
        self.assertIn("'{}'".format(rc_path), output)
        self.assertIn(line + "  # vidfunnel", output)
        self.assertEqual(rc_path.read_text(), line + "  # vidfunnel\n")
        self.do_install(shell=shell, yes=True)
        self.assertEqual(rc_path.read_text(), line + "  # vidfunnel\n")
        self.assertIn("new", output)

    def test_bash_line_appended_once(self):
        self.check_shell("/bin/bash", self.home / ".bashrc", POSIX_LINE)

    def test_zsh_line_appended_once(self):
        self.check_shell("/bin/zsh", self.home / ".zprofile", POSIX_LINE)

    def test_fish_line_appended_once(self):
        self.check_shell("/usr/bin/fish", self.home / ".config" / "fish" / "config.fish",
                         FISH_LINE)

    def test_line_appended_after_existing_content_without_newline(self):
        bashrc = self.home / ".bashrc"
        bashrc.write_text("alias ll='ls -l'")
        self.do_install(shell="/bin/bash", yes=True)
        self.assertEqual(bashrc.read_text(),
                         "alias ll='ls -l'\n" + POSIX_LINE + "  # vidfunnel\n")

    def test_answer_no_appends_nothing(self):
        code, output = self.do_install(shell="/bin/bash", yes=False, answer="n\n")
        self.assertEqual(code, 0)
        self.assertIn("Append this line? [y/N]", output)
        self.assertFalse((self.home / ".bashrc").exists())

    def test_empty_answer_appends_nothing(self):
        bashrc = self.home / ".bashrc"
        bashrc.write_text("existing line\n")
        self.do_install(shell="/bin/bash", yes=False, answer="")
        self.assertEqual(bashrc.read_text(), "existing line\n")

    def test_answer_yes_appends(self):
        self.do_install(shell="/bin/bash", yes=False, answer="y\n")
        self.assertEqual((self.home / ".bashrc").read_text(), POSIX_LINE + "  # vidfunnel\n")

    def test_no_offer_when_already_on_path(self):
        code, output = self.do_install(shell="/bin/bash", path_env=str(self.user_bin) + ":/bin")
        self.assertEqual(code, 0)
        self.assertNotIn("Append", output)
        self.assertFalse((self.home / ".bashrc").exists())

    def test_unknown_shell_prints_line_and_edits_nothing(self):
        code, output = self.do_install(shell="/bin/tcsh", yes=True)
        self.assertEqual(code, 0)
        self.assertIn("'/bin/tcsh'", output)
        self.assertEqual(sorted(path.name for path in self.home.iterdir()), [".local"])


class UninstallTests(InstallerTestCase):
    def test_uninstall_removes_symlink_and_marked_line_only(self):
        bashrc = self.home / ".bashrc"
        bashrc.write_text("first line\n" + POSIX_LINE + "\nalias vf=vidfunnel  # vidfunnel\n")
        self.do_install(shell="/bin/bash", yes=True)
        bashrc.write_text(bashrc.read_text() + "last line\n")
        code, output = self.do_uninstall()
        self.assertEqual(code, 0)
        self.assertFalse(os.path.lexists(str(self.target)))
        # The unmarked copy of the line and other marked lines are the user's own and stay.
        self.assertEqual(bashrc.read_text(), "first line\n" + POSIX_LINE
                         + "\nalias vf=vidfunnel  # vidfunnel\nlast line\n")
        self.assertIn("Removed '{}'".format(self.target), output)

    def test_uninstall_removes_marked_lines_from_every_rc_file(self):
        zprofile = self.home / ".zprofile"
        fish_config = self.home / ".config" / "fish" / "config.fish"
        installer.add_path_line(zprofile, POSIX_LINE)
        installer.add_path_line(fish_config, FISH_LINE)
        fish_config.write_text("set -x EDITOR vim\n" + fish_config.read_text())
        code, _output = self.do_uninstall()
        self.assertEqual(code, 0)
        self.assertEqual(zprofile.read_text(), "")
        self.assertEqual(fish_config.read_text(), "set -x EDITOR vim\n")

    def test_uninstall_leaves_foreign_symlink_alone(self):
        self.user_bin.mkdir(parents=True)
        other_program = Path(self.workspace.name) / "other-program"
        other_program.write_text("#!/bin/sh\n")
        os.symlink(str(other_program), str(self.target))
        installer.add_path_line(self.home / ".bashrc", POSIX_LINE)
        code, output = self.do_uninstall()
        self.assertEqual(code, 1)
        self.assertEqual(os.readlink(str(self.target)), str(other_program))
        self.assertIn("leaving it alone", output)

    def test_uninstall_leaves_regular_file_alone(self):
        self.user_bin.mkdir(parents=True)
        self.target.write_text("someone else's script\n")
        code, _output = self.do_uninstall()
        self.assertEqual(code, 1)
        self.assertEqual(self.target.read_text(), "someone else's script\n")

    def test_uninstall_with_nothing_installed(self):
        code, output = self.do_uninstall()
        self.assertEqual(code, 0)
        self.assertIn("Nothing installed", output)

    def test_remove_path_line_keeps_file_mode(self):
        bashrc = self.home / ".bashrc"
        bashrc.write_text("keep\n")
        os.chmod(str(bashrc), 0o600)
        installer.add_path_line(bashrc, POSIX_LINE)
        self.assertTrue(installer.remove_path_line(bashrc))
        self.assertFalse(installer.remove_path_line(bashrc))
        self.assertEqual(bashrc.read_text(), "keep\n")
        self.assertEqual(os.stat(str(bashrc)).st_mode & 0o777, 0o600)


class ArgumentTests(unittest.TestCase):
    def parse(self, argv):
        parser = argparse.ArgumentParser()
        installer.add_arguments(parser)
        return parser.parse_args(argv)

    def test_defaults_and_scopes(self):
        args = self.parse(["--install-command"])
        self.assertTrue(args.install_command)
        self.assertFalse(args.uninstall_command)
        self.assertEqual(args.scope, "user")
        self.assertEqual(self.parse(["--uninstall-command", "--system"]).scope, "system")
        self.assertIsNone(args.launcher)
        self.assertEqual(self.parse(["--install-command", "--launcher", "/opt/wrapper"]).launcher,
                         "/opt/wrapper")

    def test_conflicting_options_are_rejected(self):
        with open(os.devnull, "w") as quiet:
            with contextlib.redirect_stderr(quiet):
                with self.assertRaises(SystemExit):
                    self.parse(["--install-command", "--uninstall-command"])
                with self.assertRaises(SystemExit):
                    self.parse(["--install-command", "--user", "--system"])


if __name__ == "__main__":
    unittest.main()

# End of File #
