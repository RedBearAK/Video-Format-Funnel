"""Command-line interface: argument parsing, subcommand dispatch, exit codes.

Subcommands are registered here and implemented in the module named for each
stage (HANDOFF.md section 7). Until a stage exists its subcommand says so and
exits with EXIT_ERROR, never silently.
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

# (name, one-line help), in the order HANDOFF.md section 6 lists them.
SUBCOMMANDS = (
    ("check", "report which external tools are present and how to install the missing ones"),
    ("plan", "probe and analyse sources, write records with the decisions; writes no media"),
    ("convert", "run the planned ffmpeg commands and verify the results"),
    ("verify", "re-check converted outputs and update their scores"),
    ("report", "print a sorted table from the records, redacted unless --reveal"),
    ("find", "resolve a record ID from a report to its path and status"),
    ("retire", "move verified originals to quarantine; the only stage that touches sources"),
    ("index", "build or rebuild the optional SQLite index from the records"),
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


def build_parser():
    parser = argparse.ArgumentParser(
        prog="vidfunnel",
        description="Funnel legacy video into H.264 High 4.1 + AAC fragmented MP4, "
        "with a record of every decision. Originals are touched only by 'retire'.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the package version and the version of every module, then exit",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    for name, help_text in SUBCOMMANDS:
        subparsers.add_parser(name, help=help_text)
    return parser


def main(argv=None):
    parser = build_parser()
    # parse_known_args so that 'vidfunnel plan PATH' reaches the stub and says
    # "not implemented" instead of failing on arguments the stub does not know yet.
    args, _unknown = parser.parse_known_args(argv)
    if args.version:
        print(format_version())
        return EXIT_OK
    if args.command is None:
        parser.print_help()
        return EXIT_USAGE
    print("vidfunnel: '{}' is not implemented yet".format(args.command), file=sys.stderr)
    return EXIT_ERROR

# End of File #
