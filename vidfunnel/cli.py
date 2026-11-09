"""Command-line interface: argument parsing, subcommand dispatch, exit codes.

Each subcommand lives in the module named for its stage (HANDOFF.md
section 7) and exposes `add_arguments(parser)` and `run(args) -> int`.
Modules are imported lazily by name, so a stage that is not built yet
shows up as a stub that says so and exits with EXIT_ERROR, never silently.

SIGTERM (kill, or 'podman stop', where vidfunnel runs as the container's
PID 1 and would otherwise ignore it), SIGHUP (the terminal was closed) and
SIGQUIT (Ctrl+Backslash; a PID 1 without a handler ignores both) are
handled like Ctrl+C: the handler notes the signal in plain module globals
and raises Terminated, a KeyboardInterrupt, on the main thread, so the same
finally blocks release folder locks and remove part files. It takes no lock
(see _on_sigterm). The main thread then does the rest in its except paths
(vidfunnel.interrupt.stop_workers, from run_pool() and from main()): it
sets vidfunnel.interrupt.interrupted and sends SIGTERM to the ffmpeg and
par2 children that vidfunnel.ffmpeg has registered (a signal to the Python
process alone does not reach them, unlike Ctrl+C to a terminal's process
group). main() then prints 'vidfunnel: terminated (SIGTERM)' (or SIGHUP,
SIGQUIT) and returns EXIT_ERROR. Only the first of these signals raises;
later ones do nothing, so they cannot cut that cleanup short.

SIGUSR1 drains instead (its default action would end the process): the
handler sets vidfunnel.interrupt.draining and writes one line to stderr,
and run_pool() and the sequential loops of plan, convert (--refit too),
verify and retire start no further item, while the items in progress run to
their end and are recorded as usual. main() then prints 'vidfunnel:
drained: ...' and returns the stage's own exit code; the other subcommands
have no items and say that they ran to their end. A second SIGUSR1 does nothing; SIGTERM, SIGHUP,
SIGQUIT or Ctrl+C during a drain stop the run as they always do.
"""

VERSION = "20261109"

import argparse
import importlib
import os
import pkgutil
import signal
import sys
import threading

import vidfunnel

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_DEPS = 3  # a required external tool is missing (see vidfunnel.deps)

# Environment variable read as the default for --ffmpeg, so an installed command
# can use a container's exported wrapper without the option on every call.
FFMPEG_ENV = "VIDFUNNEL_FFMPEG"

# (name, module, add_arguments function, run function, one-line help),
# in the order HANDOFF.md section 6 lists them.
SUBCOMMANDS = (
    ("check", "vidfunnel.deps", "add_arguments", "run",
     "report which external tools are present and how to install the missing ones"),
    ("plan", "vidfunnel.plan", "add_arguments", "run",
     "probe and analyse sources, write records with the decisions; writes no media"),
    ("convert", "vidfunnel.encode", "add_arguments", "run",
     "run the planned ffmpeg commands and verify the results"),
    ("verify", "vidfunnel.verify", "add_arguments", "run",
     "re-check converted outputs and update their scores"),
    ("report", "vidfunnel.report", "add_arguments", "run",
     "print the records as a report, worst first: text by default, or md, json, csv; redacted unless --reveal"),
    ("find", "vidfunnel.index", "add_find_arguments", "run_find",
     "resolve a record ID from a report to its path and status"),
    ("retire", "vidfunnel.retire", "add_arguments", "run",
     "move verified originals to quarantine; the only stage that touches sources"),
    ("index", "vidfunnel.index", "add_arguments", "run",
     "build or rebuild the optional SQLite index from the records"),
    ("stop-all", "vidfunnel.stopfile", "add_arguments", "run",
     "ask every plan, convert, verify and retire run under PATH, on every host, to finish its current items "
     "and start no more "
     "(--now: stop at once); lifted by 'resume'"),
    ("resume", "vidfunnel.stopfile", "add_resume_arguments", "run_resume",
     "remove the stop file 'stop-all' wrote under PATH, so runs can start there again"),
)

# Stages that run items: they refuse to start under a stop file at or above one of their PATHs
# (vidfunnel.stopfile.refusal), and they look for one before each item and stop between items on a drain.
STOPPABLE = ("plan", "convert", "verify", "retire")


def paths_or_current_directory(args, stage):
    """Default an empty positional PATH list to the current directory, and say so.

    plan, convert, verify and index call this: with no PATH they work on the
    directory the command was run from, like ls or git status, and print the
    resolved folder first so a run from the wrong place is obvious at once.
    retire never calls it: the only stage that moves originals keeps PATH
    required as a deliberate-action check.
    """
    if getattr(args, "paths", None):
        return list(args.paths)
    here = os.getcwd()
    if not getattr(args, "quiet", False):
        print("vidfunnel {}: no PATH given; using the current directory '{}'".format(stage, here))
    args.paths = [here]
    return args.paths


def module_versions():
    """Return (module name, VERSION) for every module in the package, sorted by name.

    A module without a VERSION attribute reports None, which --version shows as
    'unversioned' so the omission is visible rather than hidden.
    """
    versions = []
    for info in pkgutil.iter_modules(vidfunnel.__path__):
        module = importlib.import_module("vidfunnel." + info.name)
        versions.append((info.name, getattr(module, "VERSION", None)))
    return sorted(versions)


def format_version():
    lines = ["vidfunnel " + vidfunnel.VERSION]
    for name, version in module_versions():
        lines.append("  {:<12} {}".format(name, version if version else "unversioned"))
    return "\n".join(lines)


def load_stage(module_name):
    """Import a stage module, or return None when that module does not exist yet.

    Any other import failure (a bug inside an existing module) propagates.
    """
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name == module_name:
            return None
        raise


def add_global_arguments(parser):
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the package version and the version of every module, then exit",
    )
    parser.add_argument(
        "--ffmpeg",
        metavar="PATH",
        default=os.environ.get(FFMPEG_ENV) or None,
        help="ffmpeg binary or wrapper to use; ffprobe is looked up next to it "
        "(default: the {} environment variable, else ffmpeg on PATH)".format(FFMPEG_ENV),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="per-file start lines and errors only, no live progress",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="also print the ffmpeg command line before each run",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="answer yes to confirmation prompts (scripted use)",
    )


def build_parser():
    parser = argparse.ArgumentParser(
        prog="vidfunnel",
        description="Funnel legacy video into H.264 High 4.1 + AAC fragmented MP4, "
        "with a record of every decision. Originals are touched only by 'retire'.",
    )
    add_global_arguments(parser)
    installer = load_stage("vidfunnel.installer")
    if installer is not None:
        installer.add_arguments(parser)
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    for name, module_name, add_name, _run_name, help_text in SUBCOMMANDS:
        subparser = subparsers.add_parser(name, help=help_text, description=help_text)
        module = load_stage(module_name)
        if module is None:
            # Stub: swallow the arguments so the "not implemented" message is reached.
            subparser.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
        else:
            getattr(module, add_name)(subparser)
    return parser


def dispatch(args):
    for name, module_name, _add_name, run_name, _help_text in SUBCOMMANDS:
        if name != args.command:
            continue
        module = load_stage(module_name)
        if module is None:
            print("vidfunnel: '{}' is not implemented yet".format(name), file=sys.stderr)
            return EXIT_ERROR
        if name in STOPPABLE:
            from vidfunnel.stopfile import refusal
            refused = refusal(getattr(args, "paths", None) or [os.getcwd()])
            if refused is not None:
                print("vidfunnel {}: {}".format(name, refused), file=sys.stderr)
                return EXIT_ERROR
        return getattr(module, run_name)(args)
    print("vidfunnel: unknown command '{}'".format(args.command), file=sys.stderr)
    return EXIT_USAGE


# Set by the first stop signal, cleared by main() on its way out. Plain globals, not a
# threading.Event: the handler may take no lock (see _on_sigterm).
_terminating = False
_stop_signal = None  # the number of the signal that set _terminating

# Signals handled like SIGTERM, and the drain signal; a name this platform lacks is skipped.
STOP_SIGNALS = ("SIGTERM", "SIGHUP", "SIGQUIT")
DRAIN_SIGNAL = "SIGUSR1"

DRAIN_NOTICE = ("vidfunnel: SIGUSR1: finishing the items in progress, starting no more; "
                "Ctrl+C or SIGTERM stops now\n")


class Terminated(KeyboardInterrupt):
    """Raised on the main thread by the stop-signal handler; handled like Ctrl+C, reported as 'terminated'."""


def _on_sigterm(signum, frame):
    """On the first SIGTERM, SIGHUP or SIGQUIT, note it and raise Terminated; on later ones, do nothing.

    Python may run this handler nested inside another run of it, or inside
    main-thread code that holds a lock, and a handler that waited for such a
    lock would wait forever. So it takes none: it reads and assigns module
    globals, and everything that locks (signalling the children) runs in the
    except paths on the main thread. Raising only once means a second
    signal cannot interrupt the finally blocks and except paths the first
    one started; two handler runs nested in each other raise at most twice,
    and the outer exception replaces the inner one.
    """
    global _terminating, _stop_signal
    if _terminating:
        return
    _terminating = True
    _stop_signal = signum
    raise Terminated()


def _on_sigusr1(signum, frame):
    """On the first SIGUSR1, set vidfunnel.interrupt.draining and say so on stderr; later ones do nothing.

    Like _on_sigterm it takes no lock: the flag is a single attribute store,
    and the line goes straight to file descriptor 2 with os.write, not
    through sys.stderr, whose buffer has a lock. A failed write (a closed
    terminal: EIO; a reader gone: EPIPE) is dropped.
    """
    from vidfunnel.interrupt import draining
    if draining.is_set() or _terminating:
        return
    draining.set()
    try:
        os.write(2, DRAIN_NOTICE.encode("utf-8"))
    except OSError:
        pass


def install_sigterm_handler(installed=None):
    """Install _on_sigterm for STOP_SIGNALS and _on_sigusr1 for DRAIN_SIGNAL; return {signal: replaced handler}.

    Only on the main thread (signal.signal works nowhere else; None is
    returned there) and only for a signal whose action is still the default,
    so an embedding program's own handler, or a signal ignored the way nohup
    ignores SIGHUP, is left alone. `installed` (a dict) is filled as each
    handler goes in, so a caller holding it can restore the ones installed
    even when a signal interrupts this function.
    """
    if threading.current_thread() is not threading.main_thread():
        return None
    import vidfunnel.ffmpeg  # imported now, so the except paths after a signal wait on no import lock
    import vidfunnel.interrupt
    if installed is None:
        installed = {}
    wanted = [(name, _on_sigterm) for name in STOP_SIGNALS] + [(DRAIN_SIGNAL, _on_sigusr1)]
    for name, handler in wanted:
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        previous = signal.getsignal(signum)
        if previous is not signal.SIG_DFL:
            continue
        signal.signal(signum, handler)
        installed[signum] = previous
    return installed


def _print_error(text):
    """Print a line to stderr. A terminal that is gone (EIO) or a reader that is gone (EPIPE) drops it and
    points stderr at /dev/null (vidfunnel.progress._write); a closed stream, or any other failed write, drops it."""
    from vidfunnel.progress import _write
    try:
        _write(sys.stderr, text + "\n")
    except (OSError, ValueError):
        pass


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(format_version())
        return EXIT_OK
    if getattr(args, "install_command", False) or getattr(args, "uninstall_command", False):
        installer = load_stage("vidfunnel.installer")
        return installer.run(args)
    if args.command is None:
        parser.print_help()
        return EXIT_USAGE
    installed = {}
    try:
        install_sigterm_handler(installed)
        code = dispatch(args)
        _report_drain(args)
        return code
    except KeyboardInterrupt as error:
        return _report_interrupt(error)
    finally:
        while installed:
            # A signal landing here, before the previous handlers are back, raises Terminated
            # inside this finally; dispatch has already returned, so it is swallowed and retried.
            try:
                for signum, previous in list(installed.items()):
                    signal.signal(signum, previous)
                _reset_after_sigterm()
                break
            except Terminated:
                continue


def _report_interrupt(error):
    """After Ctrl+C or a stop signal: stop the workers' children (signal only), print, return EXIT_ERROR.

    A stop signal after a Ctrl+C raises Terminated in here; it is caught and
    the steps are run again, so the run still ends with the message and the
    exit code. The message is dropped when stderr can no longer be written.
    """
    from vidfunnel.interrupt import stop_workers
    while True:
        try:
            terminated = isinstance(error, Terminated) or _terminating
            if terminated:
                stop_workers()
            if not terminated:
                from vidfunnel.stopfile import stopped_now
                words = "stopped (a stop file asked for it)" if stopped_now else "interrupted"
            elif _stop_signal is None:
                words = "terminated"
            else:
                words = "terminated ({})".format(_signal_name(_stop_signal))
            _print_error("\nvidfunnel: {}".format(words))
            return EXIT_ERROR
        except Terminated as nested:
            error = nested


def _signal_name(signum):
    try:
        return signal.Signals(signum).name
    except ValueError:
        return "signal {}".format(signum)


def _report_drain(args):
    """After a SIGUSR1: the 'drained' line with the counts of vidfunnel.interrupt.drain_tally."""
    from vidfunnel.interrupt import drain_tally, draining
    if not draining.is_set():
        return
    if not drain_tally.seen:
        if args.command in STOPPABLE:
            words = "no item was left to start when the drain began; it ran to its end"
        else:
            words = "it has no items to stop between; it ran to its end"
        _print_error("vidfunnel: drained: '{}': {}".format(args.command, words))
        return
    left_words = "left planned" if args.command == "convert" and not getattr(args, "refit", False) \
        else "not started"
    _print_error("vidfunnel: drained: finished {} in progress, started no more ({} {})".format(
        drain_tally.finished, drain_tally.left, left_words))


def _reset_after_sigterm():
    """Clear what a stop signal, SIGUSR1 or a stop file left set (the flags, the interrupt event, the drain tally).

    So a later main() in this process starts clean.
    """
    global _terminating, _stop_signal
    from vidfunnel.interrupt import drain_tally, draining, interrupted
    from vidfunnel.stopfile import reset as reset_stop_files
    import vidfunnel.stopfile
    if _terminating or vidfunnel.stopfile.stopped_now:  # both set the interrupt event through stop_workers()
        interrupted.clear()
        _terminating = False
        _stop_signal = None
    draining.clear()
    drain_tally.reset()
    reset_stop_files()

# End of File #
