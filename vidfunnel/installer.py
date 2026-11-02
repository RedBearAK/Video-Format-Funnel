"""Install and uninstall the 'vidfunnel' command (HANDOFF.md section 6.9).

--install-command symlinks the repo's bin/vidfunnel launcher into a bin
directory: ~/.local/bin with --user (the default) or /usr/local/bin with
--system. Anything at the target that is not a symlink to this repo's launcher
is left alone and reported; a symlink to this launcher is re-made silently,
and a dangling symlink to a bin/vidfunnel that no longer exists (the repo was
moved) is replaced. With --user, when ~/.local/bin is not on PATH, the tool
prints the rc file and the exact line for the login shell, asks before
appending it, and marks the line with '# vidfunnel' so that
--uninstall-command can remove exactly that line again.

--uninstall-command removes the symlink only when it points at this launcher,
and the marked PATH line from whichever rc files contain it. Nothing else.
"""

VERSION = "20261102"

import os
import platform
import shlex
import subprocess
import sys
from pathlib import Path

# Same values as vidfunnel.cli (not imported, so cli can import this module freely).
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

MARKER = "# vidfunnel"
COMMAND_NAME = "vidfunnel"
USER_BIN = Path(".local") / "bin"
SYSTEM_BIN = Path("/usr/local/bin")

POSIX_PATH_LINE = 'export PATH="$HOME/.local/bin:$PATH"'
FISH_PATH_LINE = "fish_add_path -g $HOME/.local/bin"

# How each supported login shell picks up the line, said after appending it.
NEW_SHELL_ADVICE = {
    "bash": "open a new terminal (or run 'exec bash') to see the change",
    "zsh": "log out and back in, or open a new terminal window that starts a login shell, "
    "to see the change ('zsh -l' starts one in place)",
    "fish": "open a new fish shell to see the change",
}


def _marked(line):
    return "{}  {}".format(line, MARKER)


# Every line this module may write; remove_path_line removes only these.
MARKED_LINES = (_marked(POSIX_PATH_LINE), _marked(FISH_PATH_LINE))


def launcher_path():
    """Return the repo's bin/vidfunnel, found from this module's own location."""
    return Path(__file__).resolve().parent.parent / "bin" / COMMAND_NAME


def target_path(scope, home=None):
    """'user' -> ~/.local/bin/vidfunnel ; 'system' -> /usr/local/bin/vidfunnel."""
    if scope == "user":
        base = Path(home) if home is not None else Path.home()
        return base / USER_BIN / COMMAND_NAME
    if scope == "system":
        return SYSTEM_BIN / COMMAND_NAME
    raise ValueError("unknown install scope '{}' (expected 'user' or 'system')".format(scope))


def path_has(directory, path_env=None):
    """True when `directory` is one of the entries of `path_env` (default: $PATH).

    Entries are compared after expanding '~' and normalising, and also by real
    path, so '/home/me/.local/bin/' and a symlinked spelling both match.
    """
    if path_env is None:
        path_env = os.environ.get("PATH", "")
    wanted = os.path.normpath(os.path.expanduser(str(directory)))
    wanted_real = os.path.realpath(wanted)
    for entry in path_env.split(os.pathsep):
        if not entry:
            continue
        entry = os.path.normpath(os.path.expanduser(entry))
        if entry == wanted or os.path.realpath(entry) == wanted_real:
            return True
    return False


def rc_file_for_shell(shell, home):
    """Return (rc file, line) for a login shell name or path, or None if unsupported.

    bash -> ~/.bashrc, zsh -> ~/.zprofile, fish -> ~/.config/fish/config.fish.
    """
    name = os.path.basename(shell or "")
    home = Path(home)
    if name == "bash":
        return home / ".bashrc", POSIX_PATH_LINE
    if name == "zsh":
        return home / ".zprofile", POSIX_PATH_LINE
    if name == "fish":
        return home / ".config" / "fish" / "config.fish", FISH_PATH_LINE
    return None


def all_rc_files(home):
    """Every rc file add_path_line may have written to, for uninstall."""
    home = Path(home)
    return [home / ".bashrc", home / ".zprofile", home / ".config" / "fish" / "config.fish"]


def _write_text_atomically(path, text):
    """Write text to path through a temp file and os.replace, keeping the file's mode."""
    path = Path(path)
    temp = path.with_name(path.name + ".vidfunnel-tmp")
    mode = None
    if path.exists():
        mode = path.stat().st_mode & 0o7777
    with open(str(temp), "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    if mode is not None:
        os.chmod(str(temp), mode)
    os.replace(str(temp), str(path))


def add_path_line(rc_path, line):
    """Append `line  # vidfunnel` to rc_path once. False if that line is already there."""
    rc_path = Path(rc_path)
    marked = _marked(line)
    existing = ""
    if rc_path.exists():
        existing = rc_path.read_text(encoding="utf-8")
        if marked in existing.splitlines():
            return False
    else:
        rc_path.parent.mkdir(parents=True, exist_ok=True)
    if existing and not existing.endswith("\n"):
        existing += "\n"
    _write_text_atomically(rc_path, existing + marked + "\n")
    return True


def remove_path_line(rc_path):
    """Remove the line(s) add_path_line wrote, and nothing else. False if none was there."""
    rc_path = Path(rc_path)
    if not rc_path.exists():
        return False
    text = rc_path.read_text(encoding="utf-8")
    lines = text.splitlines(True)
    kept = [entry for entry in lines if entry.rstrip("\r\n") not in MARKED_LINES]
    if len(kept) == len(lines):
        return False
    _write_text_atomically(rc_path, "".join(kept))
    return True


def _say(stdout, message):
    stdout.write(message + "\n")
    stdout.flush()


def _link_text(target):
    """The raw text of the symlink at target, resolved to an absolute path."""
    raw = os.readlink(str(target))
    if not os.path.isabs(raw):
        raw = os.path.join(os.path.dirname(str(target)), raw)
    return os.path.normpath(raw)


def _inspect_target(target, launcher):
    """Classify what is at target.

    Returns (state, description): state is 'absent', 'ours', 'stale' (dangling
    symlink to an old bin/vidfunnel), or 'foreign'.
    """
    target = Path(target)
    if not os.path.lexists(str(target)):
        return "absent", "nothing"
    if target.is_symlink():
        pointed = _link_text(target)
        if os.path.realpath(pointed) == os.path.realpath(str(launcher)):
            return "ours", "a symlink to '{}'".format(pointed)
        parts = Path(pointed).parts
        if not os.path.exists(pointed) and parts[-2:] == ("bin", COMMAND_NAME):
            return "stale", "a dangling symlink to '{}'".format(pointed)
        if os.path.exists(pointed):
            return "foreign", "a symlink to '{}'".format(pointed)
        return "foreign", "a dangling symlink to '{}'".format(pointed)
    if target.is_dir():
        return "foreign", "a directory"
    return "foreign", "a file that is not a symlink"


def _make_symlink(launcher, target):
    """Create or replace target as a symlink to launcher, atomically."""
    temp = target.with_name(target.name + ".vidfunnel-tmp")
    if os.path.lexists(str(temp)):
        os.unlink(str(temp))
    os.symlink(str(launcher), str(temp))
    os.replace(str(temp), str(target))


def _is_macos(platform_name):
    name = platform_name if platform_name is not None else platform.system()
    return name == "Darwin"


def _run_privileged(argv_list, stdout, runner):
    """Run each argv through sudo, printing each command first. True when all succeed."""
    for argv in argv_list:
        command = ["sudo"] + list(argv)
        _say(stdout, "running: {}".format(" ".join(shlex.quote(part) for part in command)))
        result = runner(command)
        if result.returncode != 0:
            _say(stdout, "vidfunnel: '{}' failed with exit code {}".format(
                " ".join(command), result.returncode))
            return False
    return True


def _place_symlink(launcher, target, scope, stdout, platform_name, runner):
    """Make the symlink, with sudo for --system only when the directory is not writable.

    Linux: checks writability and uses sudo when needed. macOS: tries without sudo
    first and falls back to sudo on a permission error.
    """
    directory = target.parent
    if scope == "user":
        directory.mkdir(parents=True, exist_ok=True)
        _make_symlink(launcher, target)
        return True
    try_direct = _is_macos(platform_name) or (
        directory.is_dir() and os.access(str(directory), os.W_OK))
    if try_direct:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            _make_symlink(launcher, target)
            return True
        except PermissionError as error:
            _say(stdout, "no permission to write '{}' ({}); trying with sudo".format(
                directory, error.strerror))
    return _run_privileged(
        [["mkdir", "-p", str(directory)], ["ln", "-sfn", str(launcher), str(target)]],
        stdout, runner)


def _remove_symlink(target, scope, stdout, platform_name, runner):
    directory = target.parent
    try_direct = scope == "user" or _is_macos(platform_name) or os.access(str(directory), os.W_OK)
    if try_direct:
        try:
            os.unlink(str(target))
            return True
        except PermissionError as error:
            if scope == "user":
                raise
            _say(stdout, "no permission to write '{}' ({}); trying with sudo".format(
                directory, error.strerror))
    return _run_privileged([["rm", "-f", str(target)]], stdout, runner)


def _default_runner(command):
    return subprocess.run(command)


def _offer_path_line(home, environ, yes, stdin, stdout):
    """After a --user install: offer the PATH line when ~/.local/bin is not on PATH."""
    bin_dir = Path(home) / USER_BIN
    if path_has(bin_dir, environ.get("PATH", "")):
        return EXIT_OK
    shell = environ.get("SHELL", "")
    _say(stdout, "'{}' is not on PATH.".format(bin_dir))
    choice = rc_file_for_shell(shell, home)
    if choice is None:
        _say(stdout, "Login shell '{}' is not one this tool edits (bash, zsh, fish). Add this "
             "line, or your shell's equivalent, to its startup file yourself:".format(shell))
        _say(stdout, "    " + POSIX_PATH_LINE)
        return EXIT_OK
    rc_path, line = choice
    shell_name = os.path.basename(shell)
    _say(stdout, "For {}, this line would be appended to '{}':".format(shell_name, rc_path))
    _say(stdout, "    " + _marked(line))
    if not yes:
        stdout.write("Append this line? [y/N] ")
        stdout.flush()
        answer = (stdin.readline() if stdin is not None else "").strip().lower()
        if answer not in ("y", "yes"):
            _say(stdout, "Not appended. '{}' is installed but will be found only by its full "
                 "path until '{}' is on PATH.".format(COMMAND_NAME, bin_dir))
            return EXIT_OK
    if add_path_line(rc_path, line):
        _say(stdout, "Appended to '{}'.".format(rc_path))
    else:
        _say(stdout, "'{}' already has the line.".format(rc_path))
    _say(stdout, "For {}, {}.".format(shell_name, NEW_SHELL_ADVICE[shell_name]))
    return EXIT_OK


def install(scope="user", yes=False, home=None, stdin=None, stdout=None,
            bin_dir=None, environ=None, platform_name=None, runner=None):
    """Symlink the launcher into the bin directory for scope. Returns an exit code.

    bin_dir, environ, platform_name and runner exist for tests: they replace the
    target directory, os.environ, platform.system() and the sudo runner.
    """
    stdout = stdout if stdout is not None else sys.stdout
    stdin = stdin if stdin is not None else sys.stdin
    environ = environ if environ is not None else os.environ
    runner = runner if runner is not None else _default_runner
    home = Path(home) if home is not None else Path.home()
    launcher = launcher_path()
    if not launcher.is_file():
        _say(stdout, "vidfunnel: the launcher '{}' is missing; cannot install.".format(launcher))
        return EXIT_ERROR
    target = Path(bin_dir) / COMMAND_NAME if bin_dir is not None else target_path(scope, home)

    state, description = _inspect_target(target, launcher)
    if state == "foreign":
        _say(stdout, "vidfunnel: '{}' already exists and is {}; not overwriting it. Remove it "
             "yourself if it should be replaced.".format(target, description))
        return EXIT_ERROR
    if state == "ours":
        _say(stdout, "'{}' is already installed as {}.".format(target, description))
    else:
        if state == "stale":
            _say(stdout, "Replacing {} at '{}' (the repo was moved).".format(description, target))
        code = _install_link(launcher, target, scope, stdout, platform_name, runner)
        if code != EXIT_OK:
            return code

    if scope == "user" and bin_dir is None:
        return _offer_path_line(home, environ, yes, stdin, stdout)
    return EXIT_OK


def _install_link(launcher, target, scope, stdout, platform_name, runner):
    """Place the symlink and check the result. Returns an exit code."""
    try:
        placed = _place_symlink(launcher, target, scope, stdout, platform_name, runner)
    except OSError as error:
        _say(stdout, "vidfunnel: could not create '{}': {}".format(target, error))
        return EXIT_ERROR
    if not placed:
        return EXIT_ERROR

    # Verify rather than trust: the target must now be a symlink to this launcher.
    if _inspect_target(target, launcher)[0] != "ours":
        _say(stdout, "vidfunnel: '{}' is not a symlink to '{}' after installing.".format(
            target, launcher))
        return EXIT_ERROR
    _say(stdout, "Installed '{}' -> '{}'.".format(target, launcher))
    return EXIT_OK


def uninstall(scope="user", home=None, stdout=None, bin_dir=None, platform_name=None,
              runner=None):
    """Remove the symlink if it points at this launcher, and the marked PATH lines."""
    stdout = stdout if stdout is not None else sys.stdout
    runner = runner if runner is not None else _default_runner
    home = Path(home) if home is not None else Path.home()
    launcher = launcher_path()
    target = Path(bin_dir) / COMMAND_NAME if bin_dir is not None else target_path(scope, home)

    state, description = _inspect_target(target, launcher)
    if state in ("foreign", "stale"):
        _say(stdout, "vidfunnel: '{}' is {}, not a symlink to '{}'; leaving it alone.".format(
            target, description, launcher))
        return EXIT_ERROR
    code = EXIT_OK
    if state == "absent":
        _say(stdout, "Nothing installed at '{}'.".format(target))
    else:
        try:
            removed = _remove_symlink(target, scope, stdout, platform_name, runner)
        except OSError as error:
            _say(stdout, "vidfunnel: could not remove '{}': {}".format(target, error))
            removed = False
        if removed and not os.path.lexists(str(target)):
            _say(stdout, "Removed '{}'.".format(target))
        else:
            if removed:
                _say(stdout, "vidfunnel: '{}' is still there after removing it.".format(target))
            code = EXIT_ERROR

    if scope == "user":
        for rc_path in all_rc_files(home):
            try:
                if remove_path_line(rc_path):
                    _say(stdout, "Removed the '{}' PATH line from '{}'.".format(MARKER, rc_path))
            except OSError as error:
                _say(stdout, "vidfunnel: could not edit '{}': {}".format(rc_path, error))
                code = EXIT_ERROR
    return code


def add_arguments(parser):
    """Add --install-command, --uninstall-command, --user and --system to parser."""
    group = parser.add_argument_group("installing the command")
    actions = group.add_mutually_exclusive_group()
    actions.add_argument(
        "--install-command", action="store_true",
        help="symlink bin/vidfunnel into ~/.local/bin (or /usr/local/bin with --system)")
    actions.add_argument(
        "--uninstall-command", action="store_true",
        help="remove that symlink and the '# vidfunnel' PATH line, nothing else")
    scopes = group.add_mutually_exclusive_group()
    scopes.add_argument(
        "--user", dest="scope", action="store_const", const="user",
        help="install into ~/.local/bin (the default)")
    scopes.add_argument(
        "--system", dest="scope", action="store_const", const="system",
        help="install into /usr/local/bin (sudo on Linux when that is not writable)")
    parser.set_defaults(scope="user")


def run(args):
    """Dispatch --install-command / --uninstall-command. Returns an exit code."""
    scope = getattr(args, "scope", None) or "user"
    if getattr(args, "install_command", False):
        return install(scope=scope, yes=bool(getattr(args, "yes", False)))
    if getattr(args, "uninstall_command", False):
        return uninstall(scope=scope)
    sys.stderr.write("vidfunnel: installer.run called without --install-command or "
                     "--uninstall-command\n")
    return EXIT_USAGE

# End of File #
