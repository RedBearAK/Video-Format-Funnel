"""`check`: which external tools are present, and how to install the missing ones.

HANDOFF.md sections 6.1 and 8. The ffmpeg items are tested against the build
itself (its encoder, filter, bitstream-filter and demuxer listings), never
inferred from the version string, because whether a build has `dvdvideo` is a
matter of distro build flags. `plan`, `convert` and `verify` call require()
first and refuse with the same table and hint when a required item is missing.
"""

VERSION = "20261107"

import os
import platform as platform_module
import shutil
import sys
from collections import namedtuple

from vidfunnel.cli import EXIT_DEPS, EXIT_OK
from vidfunnel.ffmpeg import (
    FfmpegError,
    ToolNotFound,
    Tools,
    capabilities,
    find_ffmpeg,
    find_ffprobe,
    run as run_tool,
    sibling_path,
)

Item = namedtuple("Item", "name required present detail")
CheckResult = namedtuple("CheckResult", "items ok platform tools")

MINIMUM_FFMPEG_MAJOR = 6
REQUIRED_ENCODERS = ("libx264", "aac")
REQUIRED_FILTERS = ("idet", "bwdif", "fieldmatch", "cropdetect", "setparams")
REQUIRED_BSFS = ("h264_metadata",)
OPTIONAL_PROGRAMS = ("par2", "mediainfo", "ffsubsync", "alass")
FFMPEG_ITEM_NAMES = ("ffmpeg", "ffprobe") + REQUIRED_ENCODERS + REQUIRED_FILTERS + REQUIRED_BSFS
DVD_ITEM_NAMES = ("dvdvideo", "libdvdread")
DISTROBOX_SCRIPT = "bin/vidfunnel-setup-distrobox"
OS_RELEASE = "/etc/os-release"


class DependencyError(Exception):
    """A required external tool or ffmpeg feature is missing."""


# --- platform -----------------------------------------------------------------


def _parse_os_release(text):
    fields = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        fields[key.strip()] = value
    return fields


def detect_platform(os_release_path=OS_RELEASE, system=None):
    """{'os': 'linux'|'macos'|'other', 'id', 'like': [...], 'pretty'}.

    Linux reads ID, ID_LIKE and PRETTY_NAME from os-release; macOS uses
    platform.mac_ver(). `os_release_path` and `system` exist for tests.
    """
    system = system if system is not None else sys.platform
    if system.startswith("linux"):
        try:
            with open(os_release_path, encoding="utf-8", errors="replace") as handle:
                fields = _parse_os_release(handle.read())
        except OSError:
            fields = {}
        return {
            "os": "linux",
            "id": fields.get("ID", ""),
            "like": fields.get("ID_LIKE", "").split(),
            "pretty": fields.get("PRETTY_NAME", "") or fields.get("NAME", "") or "Linux",
        }
    if system == "darwin":
        release = platform_module.mac_ver()[0]
        return {"os": "macos", "id": "macos", "like": [], "pretty": ("macOS " + release).strip()}
    return {"os": "other", "id": system, "like": [], "pretty": system}


def platform_family(platform):
    """'fedora', 'opensuse', 'debian', 'arch', 'alpine', 'macos' or 'unknown'."""
    if platform.get("os") == "macos":
        return "macos"
    names = [platform.get("id", "")] + list(platform.get("like", []))
    for name in names:
        if name in ("fedora", "rhel", "centos"):
            return "fedora"
        if name.startswith("opensuse") or name in ("suse", "sles"):
            return "opensuse"
        if name in ("debian", "ubuntu"):
            return "debian"
        if name in ("arch", "archlinux", "manjaro"):
            return "arch"
        if name == "alpine":
            return "alpine"
    return "unknown"


# --- the check ----------------------------------------------------------------


def _find_program(name, ffmpeg):
    """An optional program: next to ffmpeg first (distrobox wrappers live together), then PATH."""
    if ffmpeg is not None:
        sibling = sibling_path(ffmpeg, name)
        if os.path.isfile(sibling) and os.access(sibling, os.X_OK):
            return sibling
    found = shutil.which(name)
    return os.path.abspath(found) if found else None


def _ffprobe_version(ffprobe):
    """('7.1.5', None) or (None, reason)."""
    try:
        completed = run_tool([ffprobe, "-hide_banner", "-version"], check=False)
    except ToolNotFound as error:
        return None, str(error)
    if completed.returncode != 0:
        return None, "'{}' -version exited with status {}".format(ffprobe, completed.returncode)
    tokens = completed.stdout.split()
    if "version" in tokens and tokens.index("version") + 1 < len(tokens):
        return tokens[tokens.index("version") + 1], None
    return None, "'{}' -version printed no version".format(ffprobe)


def check_dependencies(ffmpeg_path=None, platform=None, refresh=False):
    """Build the required/optional table for this machine. Returns CheckResult.

    `refresh` re-queries ffmpeg's capability listings and rewrites their
    on-disk cache; 'check' passes it, require() (every other stage) reads
    the cache.
    """
    platform = platform if platform is not None else detect_platform()
    items = []
    ffmpeg = None
    ffprobe = None
    caps = None

    try:
        ffmpeg = find_ffmpeg(ffmpeg_path)
    except ToolNotFound as error:
        items.append(Item("ffmpeg", True, False, str(error)))
    if ffmpeg is not None:
        try:
            caps = capabilities(Tools(ffmpeg, None), refresh=refresh)
        except (FfmpegError, ToolNotFound) as error:
            items.append(Item("ffmpeg", True, False, "'{}' could not be queried: {}".format(ffmpeg, error)))
    if caps is not None:
        version = caps["version"]
        if caps["major"] >= MINIMUM_FFMPEG_MAJOR:
            items.append(Item("ffmpeg", True, True, "{} '{}'".format(version, ffmpeg)))
        elif caps["major"] == 0:
            items.append(
                Item("ffmpeg", True, True, "{} '{}' (git build, version >= {} not verifiable)".format(
                    version or "unknown version", ffmpeg, MINIMUM_FFMPEG_MAJOR))
            )
        else:
            items.append(
                Item("ffmpeg", True, False, "{} '{}' is older than {}".format(version, ffmpeg, MINIMUM_FFMPEG_MAJOR))
            )

    if ffmpeg is not None:
        try:
            ffprobe = find_ffprobe(ffmpeg)
        except ToolNotFound as error:
            items.append(Item("ffprobe", True, False, str(error)))
    if ffprobe is not None:
        probe_version, problem = _ffprobe_version(ffprobe)
        if problem is not None:
            items.append(Item("ffprobe", True, False, problem))
            ffprobe = None
        else:
            detail = "{} '{}'".format(probe_version, ffprobe)
            if os.path.dirname(ffprobe) != os.path.dirname(ffmpeg):
                detail += " (from PATH: none next to '{}')".format(ffmpeg)
            items.append(Item("ffprobe", True, True, detail))
    elif ffmpeg is None:
        items.append(Item("ffprobe", True, False, "not looked for: ffmpeg was not found"))

    if caps is None:
        unavailable = "unknown: ffmpeg could not be queried"
        for name in REQUIRED_ENCODERS + REQUIRED_FILTERS + REQUIRED_BSFS:
            items.append(Item(name, True, False, unavailable))
        items.append(Item("dvdvideo", False, False, unavailable))
        items.append(Item("libdvdread", False, False, unavailable))
    else:
        for name in REQUIRED_ENCODERS:
            present = name in caps["encoders"]
            items.append(Item(name, True, present, "encoder" if present else "encoder missing from this ffmpeg"))
        for name in REQUIRED_FILTERS:
            present = name in caps["filters"]
            items.append(Item(name, True, present, "filter" if present else "filter missing from this ffmpeg"))
        for name in REQUIRED_BSFS:
            present = name in caps["bsfs"]
            items.append(
                Item(name, True, present, "bitstream filter" if present else "bitstream filter missing from this ffmpeg")
            )
        present = "dvdvideo" in caps["demuxers"]
        items.append(
            Item("dvdvideo", False, present,
                 "demuxer (IFO-driven DVD input)" if present
                 else "demuxer missing: DVD trees take the VOB concatenation path")
        )
        present = "--enable-libdvdread" in caps["configuration"]
        items.append(
            Item("libdvdread", False, present,
                 "--enable-libdvdread" if present else "ffmpeg not configured with --enable-libdvdread")
        )

    for name in OPTIONAL_PROGRAMS:
        found = _find_program(name, ffmpeg)
        items.append(Item(name, False, found is not None, "'{}'".format(found) if found else "not found"))

    ok = all(item.present for item in items if item.required)
    tools = Tools(ffmpeg, ffprobe) if ok else None
    return CheckResult(items, ok, platform, tools)


# --- install hints (HANDOFF 8) ------------------------------------------------

_FFMPEG_HINTS = {
    "fedora": [
        "Fedora's base ffmpeg-free has no x264. Enable RPM Fusion free and swap in its ffmpeg:",
        "  sudo dnf install https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm",
        "  sudo dnf swap ffmpeg-free ffmpeg --allowerasing",
    ],
    "opensuse": [
        "Add the Packman repository (https://en.opensuse.org/Additional_package_repositories#Packman), then:",
        "  sudo zypper dup --from packman --allow-vendor-change",
        "  sudo zypper install ffmpeg",
    ],
    "debian": ["  sudo apt install ffmpeg"],
    "arch": ["  sudo pacman -S ffmpeg"],
    "alpine": ["  sudo apk add ffmpeg"],
    "macos": ["  brew install ffmpeg"],
    "unknown": [
        "Install ffmpeg 6 or newer built with libx264, the native aac encoder, the h264_metadata",
        "bitstream filter and the idet, bwdif, fieldmatch and cropdetect filters.",
    ],
}

_DVD_HINTS = {
    "fedora": "RPM Fusion's ffmpeg 7.1.5 is built without libdvdread (tested 2026-11-02, Fedora 43), "
    "so dvdvideo is unavailable from the distro.",
    "opensuse": "Packman's ffmpeg 7.1.5 is built without libdvdread, so dvdvideo is unavailable from the distro.",
    "debian": "Debian trixie's ffmpeg 7.1.5 has dvdvideo (tested 2026-11-02); 'sudo apt install ffmpeg' from "
    "trixie or newer provides it.",
    "arch": "Arch's ffmpeg 9.0.2 has dvdvideo (tested 2026-11-02); 'sudo pacman -S ffmpeg' provides it.",
    "alpine": "Alpine's ffmpeg 8.1.3 has dvdvideo (tested 2026-11-02); 'sudo apk add ffmpeg' provides it.",
    "macos": "Homebrew's build flags have not been checked for libdvdread yet.",
    "unknown": "dvdvideo needs an ffmpeg built with libdvdread and libdvdnav.",
}

_PAR2_PACKAGE = {
    "fedora": "  sudo dnf install par2cmdline",
    "opensuse": "  sudo zypper install par2cmdline",
    "debian": "  sudo apt install par2",
    "arch": "  sudo pacman -S par2cmdline",
    "alpine": "  sudo apk add par2cmdline",
    "macos": "  brew install par2",
}

_MEDIAINFO_PACKAGE = {
    "fedora": "  sudo dnf install mediainfo",
    "opensuse": "  sudo zypper install mediainfo",
    "debian": "  sudo apt install mediainfo",
    "arch": "  sudo pacman -S mediainfo",
    "alpine": "  sudo apk add mediainfo",
    "macos": "  brew install mediainfo",
}


def install_hint(platform, missing_names):
    """The install text for the detected platform, covering only the names given. '' when none."""
    missing = set(missing_names)
    if not missing:
        return ""
    family = platform_family(platform)
    is_linux = platform.get("os") == "linux"
    sections = []
    label = platform.get("pretty") or family
    if missing.intersection(FFMPEG_ITEM_NAMES):
        sections.append(["ffmpeg on {}:".format(label)] + _FFMPEG_HINTS[family])
    if missing.intersection(DVD_ITEM_NAMES):
        lines = ["dvdvideo (optional, IFO-driven DVD input): " + _DVD_HINTS[family]]
        if is_linux:
            lines.append(
                "An Alpine distrobox provides it without replacing the host's ffmpeg: run '{}',".format(
                    DISTROBOX_SCRIPT)
            )
            lines.append("then pass the --ffmpeg line it prints.")
        else:
            lines.append("The repo's Containerfile (Alpine) has it.")
        sections.append(lines)
    if "par2" in missing:
        command = _PAR2_PACKAGE.get(family)
        sections.append(["par2 (optional, recovery sidecars):", command or "  install par2cmdline"])
    if "mediainfo" in missing:
        command = _MEDIAINFO_PACKAGE.get(family)
        sections.append(["mediainfo (optional, richer probe for odd AVI/MOV):", command or "  install mediainfo"])
    if "ffsubsync" in missing or "alass" in missing:
        lines = [
            "ffsubsync or alass (optional, subtitle sidecar alignment): no distro packages either.",
            "ffsubsync installs with pip into a venv:",
            "  python3 -m venv ~/.cache/ffsubsync-venv",
            "  ~/.cache/ffsubsync-venv/bin/pip install ffsubsync",
            "alass ships glibc binaries on GitHub, or: cargo install alass-cli",
        ]
        if is_linux:
            lines.append("'{}' also installs ffsubsync next to its ffmpeg wrapper.".format(DISTROBOX_SCRIPT))
        sections.append(lines)
    return "\n\n".join("\n".join(lines) for lines in sections)


# --- output -------------------------------------------------------------------


def format_check(result):
    """Plain-ASCII table: name, required/optional, present/missing, detail."""
    headers = ("name", "need", "status", "detail")
    rows = [
        (item.name, "required" if item.required else "optional", "present" if item.present else "MISSING",
         item.detail)
        for item in result.items
    ]
    widths = [max(len(row[column]) for row in rows + [headers]) for column in range(3)]
    lines = ["platform: {} ('{}')".format(result.platform.get("pretty", ""), result.platform.get("id", ""))]

    def format_row(row):
        return "  ".join(cell.ljust(width) for cell, width in zip(row[:3], widths)) + "  " + row[3]

    lines.append(format_row(headers))
    lines.append(format_row(tuple("-" * width for width in widths) + ("-" * len(headers[3]),)))
    lines.extend(format_row(row) for row in rows)
    required_missing = [item.name for item in result.items if item.required and not item.present]
    if required_missing:
        lines.append("required items missing: {}".format(", ".join(required_missing)))
    else:
        lines.append("all required items present")
    return "\n".join(lines)


def missing_names(result):
    return [item.name for item in result.items if not item.present]


def require(args):
    """For plan, convert and verify: the Tools to use, or print the table and hint and raise."""
    result = check_dependencies(getattr(args, "ffmpeg", None))
    if result.ok:
        return result.tools
    required_missing = [item.name for item in result.items if item.required and not item.present]
    text = format_check(result)
    hint = install_hint(result.platform, required_missing)
    print(text, file=sys.stderr)
    if hint:
        print("\n" + hint, file=sys.stderr)
    raise DependencyError("required tools missing: {}".format(", ".join(required_missing)))


def add_arguments(parser):
    """`check` has no options of its own; --ffmpeg is a global option."""
    return parser


def run(args):
    """`vidfunnel check`: print the table and the hint; EXIT_OK when every required item is present."""
    result = check_dependencies(getattr(args, "ffmpeg", None), refresh=True)
    print(format_check(result))
    hint = install_hint(result.platform, missing_names(result))
    if hint:
        print("\n" + hint)
    return EXIT_OK if result.ok else EXIT_DEPS

# End of File #
