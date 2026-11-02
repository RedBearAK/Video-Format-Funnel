"""Command-line interface: argument parsing, subcommand dispatch, exit codes.

Each subcommand lives in the module named for its stage (HANDOFF.md
section 7) and exposes `add_arguments(parser)` and `run(args) -> int`.
Modules are imported lazily by name, so a stage that is not built yet
shows up as a stub that says so and exits with EXIT_ERROR, never silently.
"""

VERSION = "20261102"

import argparse
import importlib
import pkgutil
import sys

import vidfunnel

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_DEPS = 3  # a required external tool is missing (see vidfunnel.deps)

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
     "print a sorted table from the records, redacted unless --reveal"),
    ("find", "vidfunnel.index", "add_find_arguments", "run_find",
     "resolve a record ID from a report to its path and status"),
    ("retire", "vidfunnel.retire", "add_arguments", "run",
     "move verified originals to quarantine; the only stage that touches sources"),
    ("index", "vidfunnel.index", "add_arguments", "run",
     "build or rebuild the optional SQLite index from the records"),
)


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
        default=None,
        help="ffmpeg binary or wrapper to use; ffprobe is looked up next to it (default: PATH)",
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
        "--log",
        metavar="FILE",
        default=None,
        help="append a redacted copy of the terminal output to FILE",
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
    try:
        return dispatch(args)
    except KeyboardInterrupt:
        print("\nvidfunnel: interrupted", file=sys.stderr)
        return EXIT_ERROR

# End of File #
