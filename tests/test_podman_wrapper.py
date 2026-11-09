"""Tests for bin/vidfunnel-podman, run with sh against a fake container engine.

A fake 'podman' first on PATH writes its arguments one per line to a log file
and exits 0, so no container is started. HOME is a temporary folder in every
test; nothing here reads or writes the real ~/.local or any rc file.
"""

VERSION = "20261108"

import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / "bin" / "vidfunnel-podman"
PLAIN_LAUNCHER = REPO_ROOT / "bin" / "vidfunnel"
SHELL_BINARY = shutil.which("sh")

# The fake engine writes the arguments of 'run' and 'build' one per line to
# FAKE_ENGINE_LOG and appends every invocation, one line each, to FAKE_CALL_LOG.
# Containers are folders under FAKE_STATE_DIR: 'run -d' makes one (status, exit
# code, labels and output from the FAKE_RUN_* variables), and ps, inspect, logs,
# wait, rm, stop and kill answer from and act on those folders. 'logs -f' follows
# while the status is 'running' (at most about 30 seconds) and, like podman's own,
# ends early when a stop was requested, unless it was started with --since.
FAKE_ENGINE = r"""#!/bin/sh
state="${FAKE_STATE_DIR:-/nonexistent-fake-state}"
printf '%s\n' "$*" >> "${FAKE_CALL_LOG:-/dev/null}"
last_argument() { for argument in "$@"; do last="$argument"; done; printf '%s' "$last"; }
missing() { printf 'Error: no container with name or ID "%s" found: no such container\n' "$1" >&2; exit 125; }
case "$1" in
    info) printf '%s\n' "${FAKE_RUNTIME:-crun}"; exit 0 ;;
    run|build)
        for argument in "$@"; do printf '%s\n' "$argument"; done > "$FAKE_ENGINE_LOG"
        if [ "$1" != run ] || [ "$2" != -d ]; then exit "${FAKE_ATTACHED_EXIT:-0}"; fi
        previous=""
        for argument in "$@"; do
            [ "$previous" = --name ] && name="$argument"
            previous="$argument"
        done
        box="$state/$name"
        mkdir -p "$box"
        previous=""
        for argument in "$@"; do
            if [ "$previous" = --label ]; then
                printf '%s' "${argument#*=}" > "$box/label.${argument%%=*}"
            fi
            previous="$argument"
        done
        printf '%s\n' "${FAKE_RUN_STATUS:-exited}" > "$box/status"
        printf '%s\n' "${FAKE_RUN_EXIT:-0}" > "$box/exit_code"
        printf '%s\n' "${FAKE_RUN_OUTPUT:-fake vidfunnel output}" > "$box/log"
        echo 0123456789abcdef
        exit 0
        ;;
    ps)
        [ -d "$state" ] && ls "$state"
        exit 0
        ;;
    inspect)
        name=$(last_argument "$@")
        box="$state/$name"
        [ -d "$box" ] || missing "$name"
        format=""; previous=""
        for argument in "$@"; do
            [ "$previous" = --format ] && format="$argument"
            previous="$argument"
        done
        case "$format" in
            *State.Status*) cat "$box/status" ;;
            *ExitCode*) cat "$box/exit_code" ;;
            *Labels*)
                key="${format#*\"}"; key="${key%%\"*}"
                [ -f "$box/label.$key" ] && cat "$box/label.$key"; echo
                ;;
        esac
        exit 0
        ;;
    logs)
        name=$(last_argument "$@")
        box="$state/$name"
        [ -d "$box" ] || missing "$name"
        follow=0; since=""; tail_count=""; previous=""
        for argument in "$@"; do
            [ "$argument" = -f ] && follow=1
            [ "$previous" = --since ] && since="$argument"
            [ "$previous" = --tail ] && tail_count="$argument"
            previous="$argument"
        done
        if [ -n "$tail_count" ]; then tail -n "$tail_count" "$box/log"; else cat "$box/log"; fi
        [ "$follow" -eq 1 ] || exit 0
        rounds=0
        while [ "$(cat "$box/status" 2>/dev/null)" = running ] && [ "$rounds" -lt 300 ]; do
            if [ -z "$since" ] && [ -e "$box/stop_requested" ]; then exit 1; fi
            sleep 0.1
            rounds=$((rounds + 1))
        done
        [ -n "$since" ] && [ -f "$box/after_stop" ] && cat "$box/after_stop"
        exit 0
        ;;
    wait)
        name=$(last_argument "$@")
        box="$state/$name"
        [ -d "$box" ] || missing "$name"
        rounds=0
        while [ "$(cat "$box/status")" = running ] && [ "$rounds" -lt 300 ]; do
            sleep 0.1
            rounds=$((rounds + 1))
        done
        cat "$box/exit_code"
        exit 0
        ;;
    rm)
        name=$(last_argument "$@")
        box="$state/$name"
        [ -d "$box" ] || missing "$name"
        if [ "$(cat "$box/status")" = running ]; then
            echo "Error: cannot remove container $name as it is running" >&2
            exit 2
        fi
        rm -r "$box"
        printf '%s\n' "$name"
        exit 0
        ;;
    stop|kill)
        name=$(last_argument "$@")
        box="$state/$name"
        [ -d "$box" ] || missing "$name"
        [ -n "${FAKE_STOP_IGNORED:-}" ] && exit 0
        touch "$box/stop_requested"
        printf '%s\n' "${FAKE_STOP_OUTPUT:-fake output after the stop}" > "$box/after_stop"
        sleep "${FAKE_STOP_DELAY:-0}"
        if [ "$1" = stop ]; then code="${FAKE_STOP_EXIT:-143}"; else code="${FAKE_DRAIN_EXIT:-0}"; fi
        printf '%s\n' "$code" > "$box/exit_code"
        printf 'exited\n' > "$box/status"
        printf '%s\n' "$name"
        exit 0
        ;;
esac
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
        self.call_log = base / "engine-calls.txt"
        self.state = base / "fake-containers"
        self.state.mkdir()
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
            "FAKE_CALL_LOG": str(self.call_log),
            "FAKE_STATE_DIR": str(self.state),
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


class SupplementaryGroupTests(WrapperTestCase):
    """A share writable only through a group stays writable inside the container where the engine allows it."""

    def environment(self, home=True, shell="/bin/sh", runtime="crun"):
        env = super().environment(home=home, shell=shell)
        env["FAKE_RUNTIME"] = runtime
        return env

    def run_with_runtime(self, runtime, *arguments):
        result = subprocess.run(
            [SHELL_BINARY, str(WRAPPER)] + list(arguments), cwd=str(self.workdir),
            env=self.environment(runtime=runtime), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        return result.returncode, result.stdout + result.stderr

    def host_supplementary_gids(self):
        primary = os.getgid()
        return [str(gid) for gid in os.getgroups() if gid != primary]

    def test_podman_on_crun_keeps_the_host_groups(self):
        code, output = self.run_with_runtime("crun", "check")
        self.assertEqual(code, 0, output)
        argv = self.engine_argv()
        self.assert_pair(argv, "--group-add", "keep-groups")
        self.assertNotIn("cannot keep your supplementary groups", output)

    def test_podman_on_runc_adds_no_groups_and_warns_when_the_user_has_some(self):
        code, output = self.run_with_runtime("runc", "check")
        self.assertEqual(code, 0, output)
        argv = self.engine_argv()
        self.assertNotIn("--group-add", argv)
        if self.host_supplementary_gids():
            self.assertIn("'runc' cannot keep your supplementary groups", output)
        else:
            self.assertNotIn("cannot keep your supplementary groups", output)

    def test_docker_adds_each_supplementary_group_by_number(self):
        # The PATH must not reach the real podman once the fake one is gone, so it holds only the
        # fake docker and the tools the wrapper needs.
        os.unlink(str(self.fake_bin / "podman"))
        self.write_script("docker", FAKE_ENGINE)
        tool_bin = self.fake_bin.parent / "tools-for-docker"
        tool_bin.mkdir()
        for tool in ("env", "dirname", "basename", "readlink", "mkdir", "rm", "ln", "mv", "uname", "id", "cat"):
            found = shutil.which(tool, path="/usr/bin:/bin")
            if found is None:
                self.skipTest("'{}' is not in /usr/bin or /bin".format(tool))
            os.symlink(found, str(tool_bin / tool))
        env = dict(self.environment(runtime="runc"), PATH="{}:{}".format(self.fake_bin, tool_bin))
        result = subprocess.run([SHELL_BINARY, str(WRAPPER), "check"], cwd=str(self.workdir), env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True)
        code, output = result.returncode, result.stdout + result.stderr
        self.assertEqual(code, 0, output)
        argv = self.engine_argv()
        for gid in self.host_supplementary_gids():
            self.assert_pair(argv, "--group-add", gid)
        self.assertNotIn("keep-groups", argv)
        self.assert_pair(argv, "--user", "{}:{}".format(os.getuid(), os.getgid()))


class TerminalTests(WrapperTestCase):
    """-it only when stdin and stdout are both terminals; a piped stdout gets no terminal and no stdin."""

    def run_with_terminal(self, stdin_tty, stdout_tty):
        import pty
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        stdin = slave if stdin_tty else subprocess.DEVNULL
        stdout = slave if stdout_tty else subprocess.PIPE
        result = subprocess.run([SHELL_BINARY, str(WRAPPER), "check"], cwd=str(self.workdir),
                                env=self.environment(), stdin=stdin, stdout=stdout, stderr=subprocess.PIPE,
                                universal_newlines=True)
        os.close(slave)
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.engine_argv()

    def test_terminal_on_both_sides_gets_it(self):
        self.assertIn("-it", self.run_with_terminal(stdin_tty=True, stdout_tty=True))

    def test_piped_stdout_gets_no_terminal_even_with_a_terminal_stdin(self):
        # 'vidfunnel report | less' left Kris's terminal in raw mode and ate his keystrokes (2026-11-08).
        argv = self.run_with_terminal(stdin_tty=True, stdout_tty=False)
        self.assertNotIn("-it", argv)
        self.assertNotIn("-i", argv)
        self.assertNotIn("-t", argv)

    def test_no_terminal_on_stdin_gets_none(self):
        argv = self.run_with_terminal(stdin_tty=False, stdout_tty=True)
        self.assertNotIn("-it", argv)


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

    def test_sh_install_takes_sys_for_system(self):
        if os.access("/usr/local/bin", os.W_OK):
            self.skipTest("'/usr/local/bin' is writable here; a system install would really happen")
        code, output = self.run_wrapper("--install-command", "--sys")
        self.assertNotEqual(code, 0)
        self.assertIn("sudo ln -s '{}' '/usr/local/bin/vidfunnel'".format(WRAPPER), output)
        self.assertFalse(os.path.lexists(str(self.user_link)))

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

    def test_too_short_uninstall_prefix_goes_to_the_container(self):
        """'--u' is shorter than the shortest accepted '--un', so it starts no uninstall."""
        self.user_link.parent.mkdir(parents=True)
        os.symlink(str(WRAPPER), str(self.user_link))
        code, output = self.run_wrapper("--u")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.engine_argv()[-1], "--u")
        self.assertEqual(os.readlink(str(self.user_link)), str(WRAPPER))

    def test_install_option_before_a_subcommand_word_installs_on_the_host(self):
        # '--launcher' is passed before the user's arguments, so a trailing subcommand word still parses.
        code, output = self.run_wrapper("--install-command", "--yes", "check")
        self.assertEqual(code, 0, output)
        self.assertTrue(self.user_link.is_symlink(), output)
        self.assertFalse(self.engine_log.exists(), "no container run for an install")

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

    def test_bracketed_folder_name_is_not_a_glob(self):
        """'Movie[s]' is mounted itself, not the sibling 'Movies' its name would match as a pattern."""
        bracketed = self.elsewhere / "Movie[s]"
        bracketed.mkdir()
        (self.elsewhere / "Movies").mkdir()
        (bracketed / "clip.mkv").write_bytes(b"")
        code, output = self.run_wrapper("plan", str(bracketed / "clip.mkv"))
        self.assertEqual(code, 0, output)
        self.assert_mounted(bracketed)
        self.assertNotIn("{0}:{0}".format(self.elsewhere / "Movies"), self.mounts())

    def test_cdpath_does_not_redirect_a_relative_folder(self):
        """With CDPATH naming a folder that has a same-named subfolder, 'media' is still ./media."""
        cdpath_folder = self.elsewhere / "cdpath-folder"
        (cdpath_folder / "media").mkdir(parents=True)
        (self.workdir / "media").mkdir()
        result = subprocess.run(
            [SHELL_BINARY, str(WRAPPER), "plan", "media"], cwd=str(self.workdir),
            env=dict(self.environment(), CDPATH=str(cdpath_folder)), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(sorted(self.mounts()), sorted("{0}:{0}".format(path) for path in (
            self.workdir, self.state_dir, self.cache_dir)))

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

    def test_double_dash_after_index_is_not_a_database(self):
        """'--' is a prefix of '--db' but shorter than the shortest accepted '--d'."""
        not_a_database = self.elsewhere / "not-a-database-folder" / "index.sqlite"
        code, output = self.run_wrapper("index", "--", str(not_a_database))
        self.assertEqual(code, 0, output)
        self.assertEqual(self.engine_argv()[-3:], ["index", "--", str(not_a_database)])
        self.assertFalse(not_a_database.parent.exists())

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


class ContainerTestCase(WrapperTestCase):
    """Helpers for the detached runs, 'attach' and 'stop', against the fake engine's container folders."""

    def calls(self):
        if not self.call_log.exists():
            return []
        return self.call_log.read_text().splitlines()

    def expected_name(self, stage, first_path=""):
        digest = hashlib.sha256("{}\n{}\n".format(self.workdir, first_path).encode()).hexdigest()
        return "vidfunnel-{}-{}".format(stage, digest[:12])

    def make_container(self, name, status="running", exit_code=0, stage="convert", cwd=None,
                       paths="", started="2026-11-08T10:00:00Z", output="earlier output"):
        box = self.state / name
        box.mkdir()
        (box / "status").write_text(status + "\n")
        (box / "exit_code").write_text("{}\n".format(exit_code))
        (box / "log").write_text(output + "\n")
        labels = {"vidfunnel": "1", "vidfunnel.stage": stage,
                  "vidfunnel.cwd": str(self.workdir if cwd is None else cwd),
                  "vidfunnel.paths": paths, "vidfunnel.started": started}
        for key, value in labels.items():
            (box / "label.{}".format(key)).write_text(value)
        return box

    def run_with(self, arguments, **variables):
        env = dict(self.environment(), **variables)
        result = subprocess.run(
            [SHELL_BINARY, str(WRAPPER)] + list(arguments), cwd=str(self.workdir), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True)
        return result.returncode, result.stdout, result.stderr

    def start_in_background(self, arguments, **variables):
        env = dict(self.environment(), **variables)
        process = subprocess.Popen(
            [SHELL_BINARY, str(WRAPPER)] + list(arguments), cwd=str(self.workdir), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, start_new_session=True)
        self.addCleanup(self.end_process, process)
        return process

    def end_process(self, process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate()

    def wait_for_call(self, prefix, process, count=1):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if sum(1 for line in self.calls() if line.startswith(prefix)) >= count:
                return
            if process.poll() is not None:
                self.fail("the wrapper exited ({}) before '{}': {}".format(
                    process.returncode, prefix, process.communicate()))
            time.sleep(0.05)
        self.fail("no call starting with '{}' within 15 seconds: {}".format(prefix, self.calls()))

    def finish(self, process):
        try:
            stdout, stderr = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            self.fail("the wrapper did not exit within 20 seconds: {}".format(self.calls()))
        return process.returncode, stdout, stderr


class DetachedRunTests(ContainerTestCase):
    def test_convert_runs_detached_follows_and_exits_with_the_container_code(self):
        library = self.workdir / "library"
        library.mkdir()
        name = self.expected_name("convert", str(library))
        code, stdout, stderr = self.run_with(["convert", "--jobs", "2", "library"], FAKE_RUN_EXIT="3")
        self.assertEqual(code, 3, stdout + stderr)
        argv = self.engine_argv()
        self.assertEqual(argv[:6], ["run", "-d", "--name", name, "--label", "vidfunnel=1"])
        self.assertNotIn("--rm", argv)
        self.assertNotIn("-it", argv)
        self.assert_pair(argv, "--label", "vidfunnel.stage=convert")
        self.assert_pair(argv, "--label", "vidfunnel.cwd={}".format(self.workdir))
        self.assert_pair(argv, "--label", "vidfunnel.paths={}".format(library))
        self.assertTrue(any(part.startswith("vidfunnel.started=20") for part in argv), argv)
        self.assertEqual(argv[-4:], ["convert", "--jobs", "2", "library"])
        calls = [line for line in self.calls() if not line.startswith("inspect ")]
        run_index = next(index for index, line in enumerate(calls) if line.startswith("run -d"))
        self.assertEqual(calls[run_index + 1:], [
            "logs -f {}".format(name), "wait {}".format(name), "rm {}".format(name)])
        self.assertIn("fake vidfunnel output", stdout)
        self.assertIn("running detached as '{}'".format(name), stderr)
        self.assertFalse((self.state / name).exists())

    def test_plan_verify_and_refit_are_detached_too(self):
        for arguments in (["plan"], ["verify"], ["convert", "--refit"]):
            code, stdout, stderr = self.run_with(arguments)
            self.assertEqual(code, 0, stdout + stderr)
            self.assertEqual(self.engine_argv()[:2], ["run", "-d"], arguments)

    def test_check_stays_attached(self):
        code, stdout, stderr = self.run_with(["check"])
        self.assertEqual(code, 0, stdout + stderr)
        self.assertEqual(self.engine_argv()[:2], ["run", "--rm"])
        self.assertNotIn("-d", self.engine_argv())
        self.assertEqual([line.split()[0] for line in self.calls()], ["info", "run"])

    def test_help_for_a_long_stage_stays_attached(self):
        code, stdout, stderr = self.run_with(["convert", "--help"])
        self.assertEqual(code, 0, stdout + stderr)
        self.assertEqual(self.engine_argv()[:2], ["run", "--rm"])

    def test_attached_escape_hatch(self):
        code, stdout, stderr = self.run_with(["convert"], VIDFUNNEL_ATTACHED="1")
        self.assertEqual(code, 0, stdout + stderr)
        self.assertEqual(self.engine_argv()[:2], ["run", "--rm"])
        self.assertNotIn("--name", self.engine_argv())

    def test_unknown_escape_hatch_value_is_refused(self):
        code, stdout, stderr = self.run_with(["convert"], VIDFUNNEL_ATTACHED="yes")
        self.assertEqual(code, 1)
        self.assertIn("VIDFUNNEL_ATTACHED is 'yes'", stderr)
        self.assertEqual(self.calls(), [])

    def test_name_is_stable_for_the_same_folder_and_path_and_differs_for_another_path(self):
        (self.workdir / "first-library").mkdir()
        (self.workdir / "second-library").mkdir()
        names = []
        for library in ("first-library", "first-library", "second-library"):
            code, stdout, stderr = self.run_with(["plan", library])
            self.assertEqual(code, 0, stdout + stderr)
            names.append(self.engine_argv()[3])
        self.assertEqual(names[0], names[1])
        self.assertNotEqual(names[0], names[2])
        self.assertEqual(names[0], self.expected_name("plan", str(self.workdir / "first-library")))
        self.assertEqual(names[2], self.expected_name("plan", str(self.workdir / "second-library")))

    def test_exited_container_of_the_same_name_is_reported_and_removed_first(self):
        name = self.expected_name("convert")
        self.make_container(name, status="exited", exit_code=7, output="the last words of the earlier run")
        code, stdout, stderr = self.run_with(["convert"])
        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn("an earlier '{}'".format(name), stderr)
        self.assertIn("exit code 7", stderr)
        self.assertIn("    the last words of the earlier run", stderr)
        calls = self.calls()
        self.assertLess(calls.index("rm {}".format(name)),
                        next(index for index, line in enumerate(calls) if line.startswith("run -d")))
        self.assertIn("logs --tail 20 {}".format(name), calls)

    def test_running_container_on_the_same_paths_refuses_a_second_start(self):
        self.make_container("vidfunnel-convert-elsewhere", status="running", cwd="/somewhere/else",
                            paths=str(self.workdir))
        code, stdout, stderr = self.run_with(["plan"])
        self.assertEqual(code, 1, stdout + stderr)
        self.assertIn("'vidfunnel-convert-elsewhere' (convert, started 2026-11-08T10:00:00Z) is already running",
                      stderr)
        self.assertIn("attach vidfunnel-convert-elsewhere", stderr)
        self.assertFalse(any(line.startswith("run") for line in self.calls()), self.calls())

    def test_running_container_with_the_same_folder_and_paths_refuses(self):
        (self.workdir / "library").mkdir()
        self.make_container("vidfunnel-plan-samefolder", status="running",
                            paths=str(self.workdir / "library"))
        code, stdout, stderr = self.run_with(["convert", "library"])
        self.assertEqual(code, 1, stdout + stderr)
        self.assertIn("is already running", stderr)
        self.assertFalse(any(line.startswith("run") for line in self.calls()), self.calls())

    def test_running_container_of_the_same_name_refuses(self):
        name = self.expected_name("convert")
        self.make_container(name, status="running", paths="/not/the/same")
        code, stdout, stderr = self.run_with(["convert"])
        self.assertEqual(code, 1, stdout + stderr)
        self.assertIn("'{}' (convert, started 2026-11-08T10:00:00Z) is already running".format(name), stderr)
        self.assertFalse(any(line.startswith(("run", "rm", "logs")) for line in self.calls()), self.calls())

    def test_running_container_on_other_paths_does_not_refuse(self):
        (self.workdir / "library").mkdir()
        self.make_container("vidfunnel-convert-otherpaths", status="running", paths="/srv/other")
        code, stdout, stderr = self.run_with(["convert", "library"])
        self.assertEqual(code, 0, stdout + stderr)
        self.assertEqual(self.engine_argv()[:2], ["run", "-d"])


class FollowSignalTests(ContainerTestCase):
    def test_ctrl_c_stops_the_container_and_follows_it_to_its_exit(self):
        name = self.expected_name("convert")
        process = self.start_in_background(["convert"], FAKE_RUN_STATUS="running", FAKE_STOP_DELAY="1")
        self.wait_for_call("logs -f {}".format(name), process)
        os.killpg(process.pid, signal.SIGINT)
        code, stdout, stderr = self.finish(process)
        self.assertEqual(code, 143, stdout + stderr)
        calls = self.calls()
        self.assertEqual(calls.count("stop -t 30 {}".format(name)), 1, calls)
        self.assertTrue(any(line.startswith("logs -f --since ") and line.endswith(name) for line in calls),
                        calls)
        self.assertEqual(calls[-2:], ["wait {}".format(name), "rm {}".format(name)])
        self.assertIn("stopping '{}'".format(name), stderr)
        self.assertIn("fake output after the stop", stdout)

    def test_second_ctrl_c_detaches_without_another_stop(self):
        name = self.expected_name("convert")
        process = self.start_in_background(["convert"], FAKE_RUN_STATUS="running", FAKE_STOP_IGNORED="1")
        self.wait_for_call("logs -f {}".format(name), process)
        os.killpg(process.pid, signal.SIGINT)
        self.wait_for_call("stop ", process)
        os.killpg(process.pid, signal.SIGINT)
        code, stdout, stderr = self.finish(process)
        self.assertEqual(code, 130, stdout + stderr)
        calls = self.calls()
        self.assertEqual(sum(1 for line in calls if line.startswith("stop ")), 1, calls)
        self.assertNotIn("wait {}".format(name), calls)
        self.assertNotIn("rm {}".format(name), calls)
        self.assertIn("stopped following '{}' (Ctrl+C again)".format(name), stderr)
        self.assertEqual((self.state / name / "status").read_text(), "running\n")

    def test_closed_terminal_detaches_and_leaves_the_container_alone(self):
        name = self.expected_name("convert")
        process = self.start_in_background(["convert"], FAKE_RUN_STATUS="running")
        self.wait_for_call("logs -f {}".format(name), process)
        os.killpg(process.pid, signal.SIGHUP)
        code, stdout, stderr = self.finish(process)
        self.assertEqual(code, 129, stdout + stderr)
        calls = self.calls()
        self.assertFalse(any(line.startswith(("stop", "kill", "wait", "rm")) for line in calls), calls)
        self.assertEqual((self.state / name / "status").read_text(), "running\n")


class AttachTests(ContainerTestCase):
    def test_attach_without_argument_lists_the_containers(self):
        self.make_container("vidfunnel-plan-listedfirst", status="running", stage="plan", cwd="/srv/one",
                            paths="/srv/one/films")
        self.make_container("vidfunnel-convert-listedsecond", status="exited", exit_code=2, cwd="/srv/two")
        code, stdout, stderr = self.run_with(["attach"])
        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn("'vidfunnel-plan-listedfirst': plan, running, started 2026-11-08T10:00:00Z", stdout)
        self.assertIn("    in '/srv/one', paths '/srv/one/films'", stdout)
        self.assertIn("'vidfunnel-convert-listedsecond': convert, exited, exit code 2", stdout)
        self.assertIn("ps -a --filter label=vidfunnel=1 --format {{.Names}}", self.calls())
        self.assertFalse(any(line.startswith("logs") for line in self.calls()))

    def test_attach_without_argument_and_no_containers(self):
        code, stdout, stderr = self.run_with(["attach"])
        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn("No vidfunnel containers on this host", stdout)

    def test_attach_without_argument_follows_the_one_started_here(self):
        self.make_container("vidfunnel-convert-startedhere", status="exited", exit_code=4,
                            output="replayed from the start")
        self.make_container("vidfunnel-plan-startedelsewhere", status="running", cwd="/srv/other")
        code, stdout, stderr = self.run_with(["attach"])
        self.assertEqual(code, 4, stdout + stderr)
        self.assertIn("replayed from the start", stdout)
        self.assertIn("logs -f vidfunnel-convert-startedhere", self.calls())
        self.assertIn("rm vidfunnel-convert-startedhere", self.calls())

    def test_attach_by_name_follows_and_removes(self):
        self.make_container("vidfunnel-convert-byname", status="exited", exit_code=5, cwd="/srv/other",
                            output="the whole log")
        code, stdout, stderr = self.run_with(["attach", "vidfunnel-convert-byname"])
        self.assertEqual(code, 5, stdout + stderr)
        self.assertIn("the whole log", stdout)
        calls = [line for line in self.calls() if not line.startswith("inspect ")]
        self.assertEqual(calls[-3:], ["logs -f vidfunnel-convert-byname", "wait vidfunnel-convert-byname",
                                      "rm vidfunnel-convert-byname"])
        self.assertIn("'vidfunnel-convert-byname' exited with code 5; removed it", stderr)

    def test_attach_by_unique_prefix(self):
        self.make_container("vidfunnel-convert-uniquely", status="exited", cwd="/srv/other")
        self.make_container("vidfunnel-plan-another", status="exited", cwd="/srv/other")
        code, stdout, stderr = self.run_with(["attach", "vidfunnel-conv"])
        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn("logs -f vidfunnel-convert-uniquely", self.calls())

    def test_attach_by_ambiguous_prefix_names_both(self):
        self.make_container("vidfunnel-convert-ambiguousone", status="running", cwd="/srv/other")
        self.make_container("vidfunnel-convert-ambiguoustwo", status="running", cwd="/srv/other")
        code, stdout, stderr = self.run_with(["attach", "vidfunnel-convert-ambig"])
        self.assertEqual(code, 1, stdout + stderr)
        self.assertIn("'vidfunnel-convert-ambiguousone'", stderr)
        self.assertIn("'vidfunnel-convert-ambiguoustwo'", stderr)
        self.assertFalse(any(line.startswith("logs") for line in self.calls()))

    def test_attach_to_an_unknown_name(self):
        code, stdout, stderr = self.run_with(["attach", "vidfunnel-nothing"])
        self.assertEqual(code, 1, stdout + stderr)
        self.assertIn("no vidfunnel container on this host is named 'vidfunnel-nothing'", stderr)

    def test_attach_clean_removes_only_the_exited_ones(self):
        self.make_container("vidfunnel-plan-finished", status="exited", exit_code=3, stage="plan",
                            cwd="/srv/other")
        self.make_container("vidfunnel-convert-busy", status="running", cwd="/srv/other")
        code, stdout, stderr = self.run_with(["attach", "--clean"])
        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn("'vidfunnel-plan-finished' (plan, started 2026-11-08T10:00:00Z) exited with exit code 3",
                      stdout)
        self.assertIn("rm vidfunnel-plan-finished", self.calls())
        self.assertNotIn("rm vidfunnel-convert-busy", self.calls())
        self.assertFalse((self.state / "vidfunnel-plan-finished").exists())
        self.assertTrue((self.state / "vidfunnel-convert-busy").exists())

    def test_attach_follows_a_running_container_and_ctrl_c_stops_it(self):
        self.make_container("vidfunnel-verify-running", status="running", stage="verify", cwd="/srv/other")
        process = self.start_in_background(["attach", "vidfunnel-verify-running"], FAKE_STOP_DELAY="1")
        self.wait_for_call("logs -f vidfunnel-verify-running", process)
        os.killpg(process.pid, signal.SIGINT)
        code, stdout, stderr = self.finish(process)
        self.assertEqual(code, 143, stdout + stderr)
        self.assertIn("stop -t 30 vidfunnel-verify-running", self.calls())


class StopTests(ContainerTestCase):
    def test_stop_by_name_drains_and_follows(self):
        self.make_container("vidfunnel-convert-draining", status="running", cwd="/srv/other")
        code, stdout, stderr = self.run_with(["stop", "vidfunnel-convert-draining"],
                                             FAKE_STOP_OUTPUT="vidfunnel: drained")
        self.assertEqual(code, 0, stdout + stderr)
        calls = self.calls()
        kill_index = calls.index("kill --signal USR1 vidfunnel-convert-draining")
        follow = [line for line in calls[kill_index + 1:] if line.startswith(("logs", "wait", "rm"))]
        self.assertEqual(len(follow), 3, calls)
        self.assertTrue(follow[0].startswith("logs -f --since "), calls)
        self.assertEqual(follow[1:], ["wait vidfunnel-convert-draining", "rm vidfunnel-convert-draining"])
        self.assertFalse(any(line.startswith("stop") for line in calls), calls)
        self.assertIn("vidfunnel: drained", stdout)
        self.assertIn("asked 'vidfunnel-convert-draining' to drain", stderr)

    def test_stop_now_sends_sigterm_and_follows(self):
        self.make_container("vidfunnel-convert-stopnow", status="running", cwd="/srv/other")
        code, stdout, stderr = self.run_with(["stop", "--now", "vidfunnel-convert-stopnow"])
        self.assertEqual(code, 143, stdout + stderr)
        calls = self.calls()
        self.assertIn("stop -t 30 vidfunnel-convert-stopnow", calls)
        self.assertFalse(any(line.startswith("kill") for line in calls), calls)
        self.assertEqual(calls[-2:], ["wait vidfunnel-convert-stopnow", "rm vidfunnel-convert-stopnow"])

    def test_stop_without_name_picks_the_one_running_here(self):
        self.make_container("vidfunnel-plan-runninghere", status="running", stage="plan")
        self.make_container("vidfunnel-plan-finishedhere", status="exited")
        self.make_container("vidfunnel-plan-runningelsewhere", status="running", cwd="/srv/other")
        code, stdout, stderr = self.run_with(["stop"])
        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn("kill --signal USR1 vidfunnel-plan-runninghere", self.calls())

    def test_stop_without_name_and_none_here_lists_and_fails(self):
        self.make_container("vidfunnel-plan-runningelsewhere", status="running", cwd="/srv/other")
        code, stdout, stderr = self.run_with(["stop"])
        self.assertEqual(code, 1, stdout + stderr)
        self.assertIn("no vidfunnel container started in '{}' is running".format(self.workdir), stderr)
        self.assertIn("'vidfunnel-plan-runningelsewhere': convert, running", stdout)
        self.assertFalse(any(line.startswith(("kill", "stop")) for line in self.calls()))

    def test_stop_without_name_and_several_here_lists_and_fails(self):
        self.make_container("vidfunnel-plan-firsthere", status="running")
        self.make_container("vidfunnel-verify-secondhere", status="running")
        code, stdout, stderr = self.run_with(["stop"])
        self.assertEqual(code, 1, stdout + stderr)
        self.assertIn("2 vidfunnel containers started in", stderr)
        self.assertIn("'vidfunnel-plan-firsthere'", stdout)
        self.assertIn("'vidfunnel-verify-secondhere'", stdout)
        self.assertFalse(any(line.startswith(("kill", "stop")) for line in self.calls()))

    def test_stop_with_ambiguous_prefix_names_both(self):
        self.make_container("vidfunnel-convert-ambiguousone", status="running")
        self.make_container("vidfunnel-convert-ambiguoustwo", status="running")
        code, stdout, stderr = self.run_with(["stop", "--now", "vidfunnel-convert-amb"])
        self.assertEqual(code, 1, stdout + stderr)
        self.assertIn("'vidfunnel-convert-ambiguousone', 'vidfunnel-convert-ambiguoustwo'", stderr)
        self.assertFalse(any(line.startswith(("kill", "stop")) for line in self.calls()))

    def test_stop_with_a_folder_is_refused_with_the_stop_all_advice(self):
        library = self.workdir / "library"
        library.mkdir()
        elsewhere = self.workdir.parent / "library-elsewhere"
        elsewhere.mkdir()
        self.make_container("vidfunnel-convert-library", status="running")
        for arguments in (["stop", "library"], ["stop", "--now", "library"], ["stop", str(elsewhere)],
                          ["stop", "/srv/not-here"]):
            code, stdout, stderr = self.run_with(arguments)
            self.assertEqual(code, 1, stdout + stderr)
            self.assertIn("To stop every host's runs under a path use: vidfunnel stop-all {}".format(
                arguments[-1]), stderr)
            self.assertFalse(self.engine_log.exists(), arguments)
            self.assertFalse(any(line.startswith(("kill", "stop", "logs", "run")) for line in self.calls()),
                             self.calls())
        self.assertEqual((self.state / "vidfunnel-convert-library" / "status").read_text(), "running\n")

    def test_stop_all_and_resume_go_to_vidfunnel_attached(self):
        library = self.workdir / "library"
        library.mkdir()
        elsewhere = self.workdir.parent / "library-elsewhere"
        elsewhere.mkdir()
        for arguments in (["stop-all", "library"], ["stop-all", "--now", str(elsewhere)],
                          ["resume", "library", str(elsewhere)]):
            if self.call_log.exists():
                self.call_log.unlink()
            code, stdout, stderr = self.run_with(arguments)
            self.assertEqual(code, 0, stdout + stderr)
            argv = self.engine_argv()
            self.assertEqual(argv[:2], ["run", "--rm"], arguments)
            self.assertEqual(argv[-len(arguments):], arguments)
            self.assert_pair(argv, "-v", "{0}:{0}".format(self.workdir))
            if str(elsewhere) in arguments:
                self.assert_pair(argv, "-v", "{0}:{0}".format(elsewhere))
            self.assertFalse(any(line.startswith(("kill", "stop", "logs", "ps")) for line in self.calls()),
                             self.calls())

    def test_stop_with_a_name_that_is_no_folder_stays_local(self):
        self.make_container("vidfunnel-convert-notafolder", status="running")
        code, stdout, stderr = self.run_with(["stop", "vidfunnel-convert-notafolder"])
        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn("kill --signal USR1 vidfunnel-convert-notafolder", self.calls())
        self.assertFalse(self.engine_log.exists())

    def test_stop_of_an_exited_container_is_refused(self):
        self.make_container("vidfunnel-convert-alreadydone", status="exited", exit_code=0)
        code, stdout, stderr = self.run_with(["stop", "vidfunnel-convert-alreadydone"])
        self.assertEqual(code, 1, stdout + stderr)
        self.assertIn("'vidfunnel-convert-alreadydone' is not running", stderr)
        self.assertFalse(any(line.startswith(("kill", "stop")) for line in self.calls()))


if __name__ == "__main__":
    unittest.main()

# End of File #
