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

    def test_sh_install_accepts_abbreviations(self):
        code, output = self.run_wrapper("--y", "--us", "--inst")
        self.assertEqual(code, 0, output)
        self.assertIn("wrapper's own installer", output)
        self.assertEqual(os.readlink(str(self.user_link)), str(WRAPPER))
        code, output = self.run_wrapper("--uninst")
        self.assertEqual(code, 0, output)
        self.assertFalse(os.path.lexists(str(self.user_link)))

    def test_sh_install_rejects_unknown_options(self):
        code, output = self.run_wrapper("--install-command", "--launcher", "/elsewhere")
        self.assertNotEqual(code, 0)
        self.assertIn("'--launcher'", output)
        self.assertFalse(os.path.lexists(str(self.user_link)))


class InstallScanTests(WrapperTestCase):
    def test_install_after_the_value_of_ffmpeg(self):
        code, output = self.run_wrapper("--ffmpeg", "/opt/somewhere/ffmpeg", "--install-command")
        self.assertEqual(code, 0, output)
        self.assertFalse(self.engine_log.exists())
        self.assertEqual(os.readlink(str(self.user_link)), str(WRAPPER))

    def test_install_after_the_value_of_launcher(self):
        code, output = self.run_wrapper("--launcher", "/opt/somewhere/launcher", "--install-command")
        self.assertEqual(code, 0, output)
        self.assertFalse(self.engine_log.exists())
        self.assertEqual(os.readlink(str(self.user_link)), str(WRAPPER))

    def test_abbreviated_install_runs_on_the_host(self):
        code, output = self.run_wrapper("--inst")
        self.assertEqual(code, 0, output)
        self.assertFalse(self.engine_log.exists())
        self.assertEqual(os.readlink(str(self.user_link)), str(WRAPPER))

    def test_install_option_after_the_subcommand_goes_to_the_container(self):
        code, output = self.run_wrapper("check", "--install-command")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.engine_argv()[-2:], ["check", "--install-command"])
        self.assertFalse(os.path.lexists(str(self.user_link)))

    def test_install_option_after_a_double_dash_goes_to_the_container(self):
        code, output = self.run_wrapper("--", "--install-command")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.engine_argv()[-2:], ["--", "--install-command"])
        self.assertFalse(os.path.lexists(str(self.user_link)))


class NoEngineTests(WrapperTestCase):
    def setUp(self):
        super().setUp()
        self.tool_bin = self.fake_bin.parent / "tools-without-engine"
        self.tool_bin.mkdir()
        os.symlink(sys.executable, str(self.tool_bin / "python3"))
        for tool in ("env", "dirname", "basename", "readlink", "mkdir", "rm", "ln", "mv",
                     "uname", "id", "cat"):
            found = shutil.which(tool, path="/usr/bin:/bin")
            if found is None:
                self.skipTest("'{}' is not in /usr/bin or /bin".format(tool))
            os.symlink(found, str(self.tool_bin / tool))

    def run_without_engine(self, *arguments):
        result = subprocess.run(
            [SHELL_BINARY, str(WRAPPER)] + list(arguments), cwd=str(self.workdir),
            env=dict(self.environment(), PATH=str(self.tool_bin)), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        return result.returncode, result.stdout

    def test_install_and_uninstall_need_no_engine(self):
        code, output = self.run_without_engine("--install-command", "--yes")
        self.assertEqual(code, 0, output)
        self.assertEqual(os.readlink(str(self.user_link)), str(WRAPPER))
        code, output = self.run_without_engine("--uninstall-command")
        self.assertEqual(code, 0, output)
        self.assertFalse(os.path.lexists(str(self.user_link)))

    def test_running_without_an_engine_still_stops(self):
        code, output = self.run_without_engine("check")
        self.assertNotEqual(code, 0)
        self.assertIn("neither podman nor docker was found", output)

    def test_build_with_install_needs_an_engine(self):
        code, output = self.run_without_engine("--build", "--install-command")
        self.assertNotEqual(code, 0)
        self.assertIn("neither podman nor docker was found", output)
        self.assertFalse(os.path.lexists(str(self.user_link)))


class BuildThenInstallTests(WrapperTestCase):
    def test_build_runs_before_the_install(self):
        code, output = self.run_wrapper("--build", "--install-command")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.engine_argv()[0], "build")
        self.assertEqual(os.readlink(str(self.user_link)), str(WRAPPER))


class FfmpegRefusalTests(WrapperTestCase):
    def assert_refused(self, *arguments):
        code, output = self.run_wrapper(*arguments)
        self.assertEqual(code, 1, output)
        self.assertIn("the image's own ffmpeg is the only one that works inside the container",
                      output)
        self.assertFalse(self.engine_log.exists())

    def test_ffmpeg_with_a_separate_value_is_refused(self):
        self.assert_refused("--ffmpeg", "/usr/bin/ffmpeg", "check")

    def test_ffmpeg_with_an_equals_value_is_refused(self):
        self.assert_refused("--ffmpeg=/usr/bin/ffmpeg", "check")

    def test_abbreviated_ffmpeg_is_refused(self):
        self.assert_refused("--ff", "/usr/bin/ffmpeg", "check")
        self.assert_refused("--f=/usr/bin/ffmpeg", "check")


class PhysicalPathTests(WrapperTestCase):
    def setUp(self):
        super().setUp()
        self.elsewhere = self.workdir.parent / "elsewhere"
        self.real_folder = self.elsewhere / "realdir"
        self.real_folder.mkdir(parents=True)

    def mounts(self):
        argv = self.engine_argv()
        return [second for first, second in zip(argv, argv[1:]) if first == "-v"]

    def assert_mounted(self, path):
        self.assertIn("{0}:{0}".format(path), self.mounts())

    def test_symlinked_folder_argument_under_the_working_directory(self):
        os.symlink(str(self.real_folder), str(self.workdir / "linklib"))
        code, output = self.run_wrapper("plan", "linklib")
        self.assertEqual(code, 0, output)
        self.assert_mounted(self.real_folder)
        self.assertNotIn("{0}:{0}".format(self.workdir / "linklib"), self.mounts())

    def test_file_inside_a_symlinked_folder(self):
        (self.real_folder / "clip.mkv").write_bytes(b"")
        os.symlink(str(self.real_folder), str(self.workdir / "linklib"))
        code, output = self.run_wrapper("plan", "linklib/clip.mkv")
        self.assertEqual(code, 0, output)
        self.assert_mounted(self.real_folder)
        self.assertNotIn("{0}:{0}".format(self.real_folder / "clip.mkv"), self.mounts())

    def test_symlinked_file_with_a_relative_chain(self):
        (self.real_folder / "real.mkv").write_bytes(b"")
        os.symlink("realdir/real.mkv", str(self.elsewhere / "middle.mkv"))
        os.symlink(str(self.elsewhere / "middle.mkv"), str(self.workdir / "clip.mkv"))
        code, output = self.run_wrapper("plan", "clip.mkv")
        self.assertEqual(code, 0, output)
        self.assert_mounted(self.elsewhere)
        self.assertEqual(sorted(self.mounts()), sorted("{0}:{0}".format(path) for path in (
            self.workdir, self.elsewhere, self.state_dir, self.cache_dir)))

    def test_file_argument_mounts_its_folder(self):
        clip = self.elsewhere / "clip.avi"
        clip.write_bytes(b"")
        code, output = self.run_wrapper("plan", str(clip))
        self.assertEqual(code, 0, output)
        self.assert_mounted(self.elsewhere)
        self.assertNotIn("{0}:{0}".format(clip), self.mounts())

    def test_file_argument_through_a_symlinked_folder_mounts_both_folders(self):
        clip = self.elsewhere / "clip.avi"
        clip.write_bytes(b"")
        disk_link = self.workdir.parent / "disk-link"
        os.symlink(str(self.elsewhere), str(disk_link))
        code, output = self.run_wrapper("plan", str(disk_link / "clip.avi"))
        self.assertEqual(code, 0, output)
        self.assert_mounted(disk_link)
        self.assert_mounted(self.elsewhere)
        self.assertNotIn("{0}:{0}".format(clip), self.mounts())
        self.assertNotIn("{0}:{0}".format(disk_link / "clip.avi"), self.mounts())

    def test_chain_of_folder_symlinks_mounts_every_link(self):
        middle_link = self.elsewhere / "middle-link"
        os.symlink("realdir", str(middle_link))
        os.symlink(str(middle_link), str(self.workdir / "linklib"))
        code, output = self.run_wrapper("plan", "linklib")
        self.assertEqual(code, 0, output)
        self.assert_mounted(middle_link)
        self.assert_mounted(self.real_folder)

    def test_relative_link_target_is_resolved_from_the_physical_folder(self):
        inner = self.elsewhere / "inner"
        inner.mkdir()
        (self.real_folder / "real.mkv").write_bytes(b"")
        os.symlink("../realdir/real.mkv", str(inner / "clip-link.mkv"))
        os.symlink(str(inner), str(self.workdir / "deep-link"))
        code, output = self.run_wrapper("plan", "deep-link/clip-link.mkv")
        self.assertEqual(code, 0, output)
        self.assert_mounted(inner)
        self.assert_mounted(self.real_folder)

    def test_root_is_never_mounted(self):
        code, output = self.run_wrapper("plan", "/")
        self.assertEqual(code, 1, output)
        self.assertIn("refusing to mount '/'", output)
        self.assertFalse(self.engine_log.exists())

    def test_home_folders_behind_symlinks_are_mounted_physically(self):
        self.home = self.workdir / "inner-home"
        self.home.mkdir()
        cache_disk = self.elsewhere / "cache-disk"
        state_disk = self.elsewhere / "state-disk"
        cache_disk.mkdir()
        state_disk.mkdir()
        os.symlink(str(cache_disk), str(self.home / ".cache"))
        (self.home / ".local").mkdir()
        os.symlink(str(state_disk), str(self.home / ".local" / "state"))
        code, output = self.run_wrapper("check")
        self.assertEqual(code, 0, output)
        self.assert_mounted(cache_disk / "vidfunnel")
        self.assert_mounted(state_disk / "vidfunnel")
        self.assertTrue((cache_disk / "vidfunnel").is_dir())
        self.assertTrue((state_disk / "vidfunnel").is_dir())


class PathOptionTests(WrapperTestCase):
    def setUp(self):
        super().setUp()
        self.elsewhere = self.workdir.parent / "elsewhere"
        self.elsewhere.mkdir()

    def mounts(self):
        argv = self.engine_argv()
        return [second for first, second in zip(argv, argv[1:]) if first == "-v"]

    def assert_db_prepared(self, *arguments):
        db_folder = self.elsewhere / "index-folder"
        if db_folder.exists():
            db_folder.rmdir()
        code, output = self.run_wrapper(*[
            part.replace("DBPATH", str(db_folder / "index.sqlite")) for part in arguments])
        self.assertEqual(code, 0, output)
        self.assertTrue(db_folder.is_dir(), arguments)
        self.assertIn("{0}:{0}".format(db_folder), self.mounts(), arguments)

    def test_db_spellings_after_index_find_and_report(self):
        self.assert_db_prepared("index", "--db", "DBPATH")
        self.assert_db_prepared("index", "--db=DBPATH")
        self.assert_db_prepared("index", "--d", "DBPATH")
        self.assert_db_prepared("index", "--d=DBPATH")
        self.assert_db_prepared("find", "someid", "--d=DBPATH")
        self.assert_db_prepared("report", "--d", "DBPATH")

    def test_short_d_after_retire_is_not_a_database(self):
        source = self.workdir / "folder"
        source.mkdir()
        not_a_database = self.elsewhere / "not-a-database-folder" / "second-folder"
        code, output = self.run_wrapper("retire", "folder", "--d", str(not_a_database))
        self.assertEqual(code, 0, output)
        self.assertEqual(self.engine_argv()[-4:], ["retire", "folder", "--d", str(not_a_database)])
        self.assertFalse(not_a_database.parent.exists())

    def test_report_from_file_in_every_spelling(self):
        report_file = self.elsewhere / "earlier-report.json"
        report_file.write_text("{}\n")
        for spelling in (["--from", str(report_file)], ["--from={}".format(report_file)],
                         ["--fr={}".format(report_file)], ["--fro", str(report_file)]):
            code, output = self.run_wrapper("report", *spelling)
            self.assertEqual(code, 0, output)
            self.assertIn("{0}:{0}".format(self.elsewhere), self.mounts(), spelling)

    def test_find_root_with_equals(self):
        tree = self.elsewhere / "tree"
        tree.mkdir()
        code, output = self.run_wrapper("find", "someid", "--r={}".format(tree))
        self.assertEqual(code, 0, output)
        self.assertIn("{0}:{0}".format(tree), self.mounts())

    def test_quarantine_spellings_after_retire(self):
        source = self.workdir / "folder"
        source.mkdir()
        for spelling in ("--quarantine=QDIR", "--q=QDIR", "--quar=QDIR"):
            quarantine = self.elsewhere / "quarantine-{}".format(spelling.split("=")[0].strip("-"))
            code, output = self.run_wrapper(
                "retire", "folder", spelling.replace("QDIR", str(quarantine)))
            self.assertEqual(code, 0, output)
            self.assertTrue(quarantine.is_dir(), spelling)
            self.assertIn("{0}:{0}".format(quarantine), self.mounts(), spelling)

    def test_abbreviated_quarantine_before_retire_is_left_alone(self):
        quarantine = self.elsewhere / "not-a-quarantine"
        code, output = self.run_wrapper("plan", "--q={}".format(quarantine))
        self.assertEqual(code, 0, output)
        self.assertFalse(quarantine.exists())

    def test_launcher_value_is_not_the_subcommand(self):
        (self.workdir / "folder").mkdir()
        quarantine = self.elsewhere / "quarantine-after-launcher"
        code, output = self.run_wrapper(
            "--launcher", "/opt/somewhere/launcher", "retire", "folder", "--q={}".format(quarantine))
        self.assertEqual(code, 0, output)
        self.assertTrue(quarantine.is_dir())

    def test_folder_named_retire_is_not_the_subcommand(self):
        (self.workdir / "retire").mkdir()
        quarantine = self.elsewhere / "not-a-quarantine"
        code, output = self.run_wrapper("plan", "retire", "--q={}".format(quarantine))
        self.assertEqual(code, 0, output)
        self.assertFalse(quarantine.exists())


if __name__ == "__main__":
    unittest.main()

# End of File #
