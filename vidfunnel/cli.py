"""Command-line interface: argument parsing, subcommand dispatch, exit codes.

Each subcommand lives in the module named for its stage (HANDOFF.md
section 7) and exposes `add_arguments(parser)` and `run(args) -> int`.
Modules are imported lazily by name, so a stage that is not built yet
shows up as a stub that says so and exits with EXIT_ERROR, never silently.

SIGTERM (kill, or 'podman stop', where vidfunnel runs as the container's
PID 1 and would otherwise ignore it) is handled like Ctrl+C: the handler
notes it in a plain module flag and raises Terminated, a KeyboardInterrupt,
on the main thread, so the same finally blocks release folder locks and
remove part files. It takes no lock (see _on_sigterm). The main thread then
does the rest in its except paths (vidfunnel.interrupt.stop_workers, from
run_pool() and from main()): it sets vidfunnel.interrupt.interrupted and
sends SIGTERM to the ffmpeg and par2 children that vidfunnel.ffmpeg has
registered (a signal to the Python process alone does not reach them,
unlike Ctrl+C to a terminal's process group). main() then prints
'vidfunnel: terminated' and returns EXIT_ERROR. Only the first SIGTERM
raises; later ones do nothing, so they cannot cut that cleanup short.
"""

VERSION = "20261108"

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
)


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
        return getattr(module, run_name)(args)
    print("vidfunnel: unknown command '{}'".format(args.command), file=sys.stderr)
    return EXIT_USAGE


# Set by the first SIGTERM, cleared by main() on its way out. A plain bool, not a
# threading.Event: the handler may take no lock (see _on_sigterm).
_terminating = False


class Terminated(KeyboardInterrupt):
    """Raised on the main thread by the SIGTERM handler; handled like Ctrl+C, reported as 'terminated'."""


def _on_sigterm(signum, frame):
    """On the first SIGTERM, set _terminating and raise Terminated; on later ones, do nothing.

    Python may run this handler nested inside another run of it, or inside
    main-thread code that holds a lock, and a handler that waited for such a
    lock would wait forever. So it takes none: it reads and assigns a module
    global, and everything that locks (signalling the children) runs in the
    except paths on the main thread. Raising only once means a second
    SIGTERM cannot interrupt the finally blocks and except paths the first
    one started; two handler runs nested in each other raise at most twice,
    and the outer exception replaces the inner one.
    """
    global _terminating
    if _terminating:
        return
    _terminating = True
    raise Terminated()


def install_sigterm_handler():
    """Install _on_sigterm; returns the handler it replaced, or None when it installed nothing.

    Only on the main thread (signal.signal works nowhere else) and only
    while SIGTERM has its default action, so an embedding program's own
    handler, or an ignored SIGTERM, is left alone.
    """
    if threading.current_thread() is not threading.main_thread():
        return None
    import vidfunnel.ffmpeg  # imported now, so the except paths after a SIGTERM wait on no import lock
    previous = signal.getsignal(signal.SIGTERM)
    if previous is not signal.SIG_DFL:
        return None
    signal.signal(signal.SIGTERM, _on_sigterm)
    return previous


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
    previous = install_sigterm_handler()
    try:
        return dispatch(args)
    except KeyboardInterrupt as error:
        return _report_interrupt(error)
    finally:
        if previous is not None:
            while True:
                # A SIGTERM landing here, before the previous handler is back, raises Terminated
                # inside this finally; dispatch has already returned, so it is swallowed and retried.
                try:
                    signal.signal(signal.SIGTERM, previous)
                    _reset_after_sigterm()
                    break
                except Terminated:
                    continue


def _report_interrupt(error):
    """After Ctrl+C or SIGTERM: stop the workers' children (SIGTERM only), print, return EXIT_ERROR.

    A SIGTERM after a Ctrl+C raises Terminated in here; it is caught and the
    steps are run again, so the run still ends with the message and the exit code.
    """
    from vidfunnel.interrupt import stop_workers
    while True:
        try:
            terminated = isinstance(error, Terminated) or _terminating
            if terminated:
                stop_workers()
            print("\nvidfunnel: {}".format("terminated" if terminated else "interrupted"), file=sys.stderr)
            return EXIT_ERROR
        except Terminated as nested:
            error = nested


def _reset_after_sigterm():
    """Clear _terminating and the interrupt event a SIGTERM left set, so a later main() in this process starts clean."""
    global _terminating
    if _terminating:
        from vidfunnel.interrupt import interrupted
        interrupted.clear()
        _terminating = False

# End of File #
