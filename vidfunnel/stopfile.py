"""`stop-all` and `resume`: a stop request written on the share, seen by every run under it on every host.

`vidfunnel stop-all PATH...` writes '<PATH>/.vidfunnel/stop', a small JSON
file {"host", "pid", "requested", "mode", "user"}; `vidfunnel resume
PATH...` removes it. Nothing else writes or removes it, and it is not a
record or a lock. A run looks for it by walking up from an item's folder
to the file system root, one stat per level, every time it is about to
start an item (find_stop, check_item), so a request on a library's root
reaches every folder below it from any host that sees the share:

- mode "drain" sets vidfunnel.interrupt.draining exactly as SIGUSR1 does
  (the items in progress finish, no further item starts) and says so once;
- mode "now" acts as Ctrl+C does: it sets the interrupt event and sends
  SIGTERM to this run's children (vidfunnel.interrupt.stop_workers), and
  the item stops. It is also checked before each child process an item
  starts (check_now, from vidfunnel.ffmpeg.run and encode.write_par2), so
  it takes effect at the next item or child start; a child already running
  is stopped when another check in this run fires, not by the file itself.

A stop, once seen, holds for the rest of the run: removing the file does
not undo it. A stage that is about to start (plan, convert, verify,
retire) under a stop file at or above one of its PATHs refuses before it
takes any folder lock (refusal).
"""

VERSION = "20261108"

import getpass
import json
import os
import socket
import sys
import threading

from vidfunnel.record import RECORD_DIR, _write_json_atomic, now_iso

STOP_NAME = "stop"
MODES = ("drain", "now")

# The folder of the item this thread is working on, for check_now(); set by check_item().
_current = threading.local()

# One announcement per run; a lock, since workers may find the file at the same time (never a signal handler).
_announced_lock = threading.Lock()
_announced = set()

# Set once a 'now' stop file was acted on in this run, for vidfunnel.cli's last line.
stopped_now = False


def stop_path(folder):
    """'<folder>/.vidfunnel/stop'."""
    return os.path.join(folder, RECORD_DIR, STOP_NAME)


def find_stop(folder):
    """(stop file path, its contents as a dict) of the nearest stop file at or above `folder`, or (None, None).

    Walks the absolute path's parents up to the root with one os.stat per
    level. A file that exists but cannot be read or parsed counts as a
    drain request, so a half-written or damaged file still stops new work.
    """
    current = os.path.abspath(os.fspath(folder))
    while True:
        candidate = stop_path(current)
        try:
            os.stat(candidate)
        except OSError:
            pass
        else:
            return candidate, _read(candidate)
        parent = os.path.dirname(current)
        if parent == current:
            return None, None
        current = parent


def _read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        return {"mode": "drain", "host": None, "requested": None, "unreadable": True}
    if data.get("mode") not in MODES:
        data = dict(data, mode="drain")
    return data


def describe(path, data):
    """'from host H at T (mode)' for messages; an unreadable file says so."""
    if data.get("unreadable"):
        return "in '{}' (unreadable, taken as drain)".format(path)
    return "from host '{}' at {} ({})".format(data.get("host"), data.get("requested"), data.get("mode"))


def _announce(path, data):
    from vidfunnel.progress import _write
    key = (path, data.get("mode"))
    with _announced_lock:
        if key in _announced:
            return
        _announced.add(key)
    if data.get("mode") == "now":
        words = "stopping now"
    else:
        words = "finishing the items in progress, starting no more"
    try:
        _write(sys.stderr, "vidfunnel: stop requested {}: {}; stop file '{}'\n".format(describe(path, data), words,
                                                                                        path))
    except (OSError, ValueError):
        pass


def check_item(folder):
    """Before an item in `folder` starts: None, 'drain' or 'now' from the nearest stop file.

    Remembers `folder` for check_now() on this thread. 'drain' sets
    vidfunnel.interrupt.draining; 'now' calls stop_workers() (the interrupt
    event and SIGTERM to the children). Each is announced once per run.
    The caller starts no item after either.
    """
    from vidfunnel.interrupt import draining, stop_workers
    _current.folder = folder
    path, data = find_stop(folder)
    if path is None:
        return None
    global stopped_now
    _announce(path, data)
    if data.get("mode") == "now":
        stopped_now = True
        stop_workers()
        return "now"
    draining.set()
    return "drain"


def check_now():
    """Before a child process starts: act on a 'now' stop file above this thread's item (see check_item).

    Returns True when the run must stop; a 'drain' file sets the drain and
    returns False, since the item in progress may finish.
    """
    folder = getattr(_current, "folder", None)
    if folder is None:
        return False
    return check_item(folder) == "now"


def raise_if_stopped_now(what):
    """Before a child process starts: raise as Ctrl+C would when a 'now' stop file applies (see check_now).

    KeyboardInterrupt on the main thread (the sequential path), Interrupted
    on a worker, the exception each of them sees after Ctrl+C.
    """
    from vidfunnel.interrupt import Interrupted
    if not check_now():
        return
    if threading.current_thread() is threading.main_thread():
        raise KeyboardInterrupt()
    raise Interrupted("a stop file asks this run to stop now; '{}' was not started".format(what))


def forget_item():
    """This thread works on no item any more."""
    _current.folder = None


def reset():
    """Forget the announcements and stopped_now, so a later run in this process starts clean."""
    global stopped_now
    with _announced_lock:
        _announced.clear()
    stopped_now = False
    forget_item()


def refusal(paths):
    """The refusal message for a stage about to start under a stop file at or above one of `paths`, or None."""
    for given in paths:
        start = os.path.abspath(os.fspath(given))
        if not os.path.isdir(start):
            start = os.path.dirname(start)
        path, data = find_stop(start)
        if path is not None:
            directory = os.path.dirname(os.path.dirname(path))
            return ("a stop was requested {} for '{}'; stop file '{}'; nothing was started; "
                    "remove it with: vidfunnel resume '{}' (or delete that file)".format(
                        describe(path, data), directory, path, directory))
    return None


# ---------------------------------------------------------------- stop-all and resume


def add_arguments(parser):
    parser.add_argument("paths", nargs="+", metavar="PATH",
                        help="folders whose runs to stop: every run working at or below PATH, on any host")
    parser.add_argument("--now", action="store_true",
                        help="stop at once (as Ctrl+C), instead of finishing the items in progress")


def run(args):
    """Write '<PATH>/.vidfunnel/stop' for each PATH (atomically); one line per file."""
    from vidfunnel.cli import EXIT_ERROR, EXIT_OK
    mode = "now" if args.now else "drain"
    try:
        user = getpass.getuser() or None
    except Exception:  # getpass.getuser raises when no user name can be found at all
        user = None
    content = {"host": socket.gethostname(), "pid": os.getpid(), "requested": now_iso(), "mode": mode, "user": user}
    code = EXIT_OK
    for given in args.paths:
        if not os.path.isdir(given):
            print("vidfunnel stop-all: '{}' is not a folder".format(given), file=sys.stderr)
            code = EXIT_ERROR
            continue
        path = stop_path(given)
        try:
            _write_json_atomic(path, content)
        except OSError as error:
            print("vidfunnel stop-all: cannot write '{}': {}".format(path, error), file=sys.stderr)
            code = EXIT_ERROR
            continue
        print("vidfunnel stop-all: wrote '{}' ({}): runs at or below '{}' on every host {}; lift it with: "
              "vidfunnel resume '{}'".format(
                  path, mode, given, "stop at their next item or child process" if mode == "now"
                  else "finish their items in progress and start no more", given))
    return code


def add_resume_arguments(parser):
    parser.add_argument("paths", nargs="+", metavar="PATH", help="folders whose stop file to remove")


def run_resume(args):
    """Remove '<PATH>/.vidfunnel/stop' for each PATH; say so when there is none."""
    from vidfunnel.cli import EXIT_ERROR, EXIT_OK
    code = EXIT_OK
    for given in args.paths:
        path = stop_path(given)
        try:
            os.unlink(path)
        except FileNotFoundError:
            print("vidfunnel resume: no stop file at '{}'".format(path))
            continue
        except OSError as error:
            print("vidfunnel resume: cannot remove '{}': {}".format(path, error), file=sys.stderr)
            code = EXIT_ERROR
            continue
        print("vidfunnel resume: removed '{}'; runs started from now on proceed".format(path))
        above, _data = find_stop(os.path.dirname(os.path.abspath(given)))
        if above is not None:
            print("vidfunnel resume: note: another stop file still applies here: '{}'".format(above))
    return code

# End of File #
