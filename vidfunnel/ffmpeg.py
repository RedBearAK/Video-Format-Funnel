"""ffmpeg and ffprobe: discovery, capabilities, the subprocess runner, -progress parsing.

HANDOFF.md sections 4.1, 6.10, 7 and 8. Every external call goes through run(),
with an argv list (never a shell string) and stdin closed, so ffmpeg can never
wait on the terminal. ffprobe is looked up next to the ffmpeg in use, under the
same name pattern, so a pair of distrobox wrappers in a directory of their own
works without shadowing the host's tools.
"""

VERSION = "20261107"

import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
from collections import namedtuple
from pathlib import Path

from vidfunnel.interrupt import Interrupted, child_interrupted, interrupted
from vidfunnel.interrupt import check as check_interrupted
from vidfunnel.progress import say


class ToolNotFound(Exception):
    """ffmpeg or ffprobe could not be found or is not executable."""


class FfmpegError(Exception):
    """An ffmpeg or ffprobe run failed. Carries argv, returncode and the whole stderr."""

    def __init__(self, message, argv=None, returncode=None, stderr=""):
        super().__init__(message)
        self.argv = list(argv) if argv is not None else []
        self.returncode = returncode
        self.stderr = stderr


Tools = namedtuple("Tools", "ffmpeg ffprobe")
CompletedRun = namedtuple("CompletedRun", "argv returncode stdout stderr")
DecodeResult = namedtuple("DecodeResult", "returncode message_classes stderr")

# Lines of stderr quoted in an FfmpegError message; the whole text is on .stderr.
ERROR_TAIL_LINES = 5

_capabilities_cache = {}

# On-disk copy of the capability listings, so the six listing calls that produce
# them run once per ffmpeg build instead of once per run (through a distrobox
# wrapper each call costs more than a second). Keyed by the binary's path, size,
# mtime and the SHA-256 of its first and last MiB, plus the text of `-version`
# (one call per run), so a changed, replaced or upgraded ffmpeg is queried again
# even behind an unchanged wrapper script. 'check' always queries again and
# refreshes the cache.
CAPABILITIES_CACHE_FORMAT = 1
CAPABILITIES_CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or "~/.cache").expanduser() / "vidfunnel" / "capabilities"
_SET_FIELDS = ("encoders", "decoders", "muxers", "demuxers", "filters", "bsfs")


# --- discovery ---------------------------------------------------------------


def _is_executable_file(path):
    return os.path.isfile(path) and os.access(path, os.X_OK)


def find_ffmpeg(ffmpeg_path=None):
    """Return the absolute path of ffmpeg: the given path or name, or 'ffmpeg' on PATH.

    The path is made absolute but symlinks are not resolved: a wrapper's own
    directory is where its ffprobe sibling lives.
    """
    if ffmpeg_path is None:
        found = shutil.which("ffmpeg")
        if found is None:
            raise ToolNotFound("ffmpeg was not found on PATH ('{}')".format(os.environ.get("PATH", "")))
        return os.path.abspath(found)
    given = os.path.expanduser(os.fspath(ffmpeg_path))
    if os.sep in given or os.path.exists(given):
        if not os.path.exists(given):
            raise ToolNotFound("ffmpeg '{}' does not exist".format(given))
        if not _is_executable_file(given):
            raise ToolNotFound("ffmpeg '{}' is not an executable file".format(given))
        return os.path.abspath(given)
    found = shutil.which(given)
    if found is None:
        raise ToolNotFound("ffmpeg '{}' was not found on PATH ('{}')".format(given, os.environ.get("PATH", "")))
    return os.path.abspath(found)


def sibling_path(ffmpeg, name):
    """Path of a tool next to ffmpeg under the same name pattern, or None.

    'ffmpeg' in the basename is replaced by `name` ('/opt/x/ffmpeg-7' and
    'ffprobe' give '/opt/x/ffprobe-7'). For tools without the pattern
    (ffsubsync, par2) the name is used as is in ffmpeg's directory.
    """
    directory, base = os.path.split(ffmpeg)
    if name == "ffprobe":
        if "ffmpeg" not in base:
            return None
        return os.path.join(directory, base.replace("ffmpeg", "ffprobe", 1))
    return os.path.join(directory, name)


def find_ffprobe(ffmpeg):
    """Return ffprobe next to `ffmpeg` (same name pattern), falling back to PATH."""
    sibling = sibling_path(ffmpeg, "ffprobe")
    if sibling is not None and _is_executable_file(sibling):
        return sibling
    found = shutil.which("ffprobe")
    if found is not None:
        return os.path.abspath(found)
    raise ToolNotFound(
        "ffprobe was not found next to '{}' (looked for '{}') nor on PATH ('{}')".format(
            ffmpeg, sibling if sibling is not None else "no 'ffmpeg' in the name", os.environ.get("PATH", "")
        )
    )


def find_tools(ffmpeg_path=None):
    """Return Tools(ffmpeg, ffprobe) as absolute paths, or raise ToolNotFound."""
    ffmpeg = find_ffmpeg(ffmpeg_path)
    return Tools(ffmpeg, find_ffprobe(ffmpeg))


# --- version and capabilities -------------------------------------------------


def parse_version(text):
    """Version tuple from 'ffmpeg version 7.1.5 ...', '7.1.5-0+deb13u1' or 'n9.0.2'.

    A git build ('N-112345-gabcdef') or anything unparseable gives (0,); the
    caller keeps the raw string.
    """
    tokens = text.split()
    if not tokens:
        return (0,)
    token = tokens[0]
    if "version" in tokens:
        position = tokens.index("version")
        if position + 1 < len(tokens):
            token = tokens[position + 1]
    if token.startswith("n"):
        token = token[1:]
    match = re.match(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", token)
    if match is None:
        return (0,)
    return tuple(int(part) for part in match.groups() if part is not None)


def _version_string(version_output):
    """The raw version token from the first line of `-version` output, or ''."""
    first_line = version_output.splitlines()[0] if version_output.strip() else ""
    tokens = first_line.split()
    if "version" in tokens:
        position = tokens.index("version")
        if position + 1 < len(tokens):
            return tokens[position + 1]
    return ""


def _parse_configuration(version_output):
    for line in version_output.splitlines():
        if line.startswith("configuration:"):
            return shlex.split(line[len("configuration:"):])
    return []


def _parse_table(output, split_commas):
    """Names from an -encoders/-decoders/-muxers/-demuxers listing.

    The listing has a legend, then a line of dashes as wide as the flags
    column, then one ' FLAGS name description' line per entry. The flags of
    the format listings may contain spaces (' D d alsa'), so the name is cut at
    the column the dash line gives, not at the first space.
    """
    names = set()
    width = None
    for line in output.splitlines():
        if width is None:
            stripped = line.strip()
            if stripped and set(stripped) == {"-"}:
                width = len(stripped)
            continue
        content = line[1:] if line.startswith(" ") else line
        rest = content[width:].split()
        if not rest:
            continue
        if split_commas:
            names.update(part for part in rest[0].split(",") if part)
        else:
            names.add(rest[0])
    return names


# ffmpeg 7 prints three flag columns (TSC), ffmpeg 8 two (TS); the name is the token
# after the flags, and the in->out column ("V->V", "|->A") marks an entry line.
_FILTER_LINE = re.compile(r"^\s*[A-Z.]{2,4}\s+(\S+)\s+\S*->\S*")


def _parse_filters(output):
    names = set()
    for line in output.splitlines():
        match = _FILTER_LINE.match(line)
        if match:
            names.add(match.group(1))
    return names


def _parse_bsfs(output):
    names = set()
    started = False
    for line in output.splitlines():
        if not started:
            started = line.strip().startswith("Bitstream filters")
            continue
        if line.strip():
            names.add(line.strip())
    return names


_CAPABILITY_QUERIES = (
    ("encoders", "-encoders"),
    ("decoders", "-decoders"),
    ("muxers", "-muxers"),
    ("demuxers", "-demuxers"),
    ("filters", "-filters"),
    ("bsfs", "-bsfs"),
)


def _binary_identity(path):
    """What the cache key is made of; None when the file is unreadable or ffmpeg does not run.

    Path, size, mtime_ns and the head/tail SHA-256 of the file, plus the text
    of `ffmpeg -version` (one call). The file alone is not enough: the
    recommended setup points --ffmpeg at a distrobox wrapper script, which
    stays byte-identical when the ffmpeg inside the box is upgraded; the
    version banner (version and configuration) changes with the build.
    """
    from vidfunnel.record import cheap_fingerprint
    try:
        fingerprint = cheap_fingerprint(path)
    except OSError:
        return None
    try:
        banner = run([path, "-hide_banner", "-version"]).stdout
    except (FfmpegError, ToolNotFound, OSError):
        return None
    identity = {"path": str(path), "version_banner": banner.strip()}
    identity.update(fingerprint)
    return identity


def _cache_file_for(identity):
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
    return CAPABILITIES_CACHE_DIR / (digest[:32] + ".json")


def _load_cached_capabilities(identity):
    """The cached listing for this exact binary, or None (missing, stale, unreadable or malformed)."""
    if identity is None:
        return None
    try:
        with open(str(_cache_file_for(identity)), "r", encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(stored, dict) or stored.get("format") != CAPABILITIES_CACHE_FORMAT:
        return None
    if stored.get("identity") != identity or not isinstance(stored.get("capabilities"), dict):
        return None
    result = dict(stored["capabilities"])
    try:
        result["version_tuple"] = tuple(result["version_tuple"])
        for field in _SET_FIELDS:
            result[field] = set(result[field])
    except (KeyError, TypeError):
        return None
    return result


def _store_cached_capabilities(identity, result):
    """Write the listing next to its identity; a failure to write is not an error."""
    if identity is None:
        return
    serialisable = dict(result)
    serialisable["version_tuple"] = list(result["version_tuple"])
    for field in _SET_FIELDS:
        serialisable[field] = sorted(result[field])
    target = _cache_file_for(identity)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".part")
        with open(str(temporary), "w", encoding="utf-8") as handle:
            json.dump({"format": CAPABILITIES_CACHE_FORMAT, "identity": identity, "capabilities": serialisable},
                      handle, indent=1, sort_keys=True)
        os.replace(str(temporary), str(target))
    except OSError:
        pass


def capabilities(tools, refresh=False):
    """What the ffmpeg in `tools` can do, parsed from its listings.

    {'version': '7.1.5', 'major': 7, 'version_tuple': (7, 1, 5),
     'configuration': [...], 'encoders', 'decoders', 'muxers', 'demuxers',
     'filters', 'bsfs': sets of names}. Raises FfmpegError when ffmpeg fails.

    Cached in this process per ffmpeg path and on disk per binary identity
    (see CAPABILITIES_CACHE_DIR); `refresh` skips both caches and rewrites
    the disk copy, which is what 'check' does.
    """
    key = tools.ffmpeg
    if not refresh and key in _capabilities_cache:
        return _capabilities_cache[key]
    identity = _binary_identity(tools.ffmpeg)
    if not refresh:
        cached = _load_cached_capabilities(identity)
        if cached is not None:
            _capabilities_cache[key] = cached
            return cached
    say("querying the capabilities of '{}' ({} calls)".format(tools.ffmpeg, 1 + len(_CAPABILITY_QUERIES)))
    if identity is not None:
        version_output = identity["version_banner"]  # the identity already ran -version
    else:
        version_output = run([tools.ffmpeg, "-hide_banner", "-version"]).stdout
    version = _version_string(version_output)
    version_tuple = parse_version(version) if version else (0,)
    result = {
        "version": version,
        "major": version_tuple[0],
        "version_tuple": version_tuple,
        "configuration": _parse_configuration(version_output),
    }
    for name, option in _CAPABILITY_QUERIES:
        output = run([tools.ffmpeg, "-hide_banner", option]).stdout
        if name == "filters":
            result[name] = _parse_filters(output)
        elif name == "bsfs":
            result[name] = _parse_bsfs(output)
        else:
            result[name] = _parse_table(output, split_commas=name in ("muxers", "demuxers"))
        if not result[name]:
            raise FfmpegError(
                "'{}' {} printed no entries; its output format was not understood".format(tools.ffmpeg, option),
                argv=[tools.ffmpeg, "-hide_banner", option],
                returncode=0,
                stderr="",
            )
    _capabilities_cache[key] = result
    _store_cached_capabilities(identity, result)
    return result


def configuration_sha256(configuration):
    """SHA-256 (hex) of a parsed configuration list, joined back with shlex.join, UTF-8."""
    return hashlib.sha256(shlex.join([str(part) for part in configuration or []]).encode("utf-8")).hexdigest()


def plan_identity(path, caps):
    """{'path', 'version', 'configuration_sha256'} of the ffmpeg at `path`, from its capabilities(). Pure.

    What plan records under stages.plan.ffmpeg, so convert can tell whether
    the ffmpeg named in the argv is still the build the plan was made with.
    """
    return {"path": str(path), "version": (caps or {}).get("version") or "",
            "configuration_sha256": configuration_sha256((caps or {}).get("configuration"))}


# current_identity() results for this process, keyed by (path, size, mtime in ns) of the file at path.
_identity_cache = {}


def current_identity(path):
    """plan_identity() of the ffmpeg at `path` as it is now, from one `-version` call per build.

    The result is kept for the rest of the process under the path, size and
    mtime of the file at `path` (os.stat, which follows symlinks), so 500
    records naming one ffmpeg cost one call, and a file replaced meanwhile
    is asked again. A path os.stat cannot read is not cached. Failures are
    not cached either. Raises FfmpegError or ToolNotFound when it does not run.
    """
    try:
        status = os.stat(path)
        key = (str(path), status.st_size, status.st_mtime_ns)
    except OSError:
        key = None
    if key is not None and key in _identity_cache:
        return dict(_identity_cache[key])
    output = run([path, "-hide_banner", "-version"]).stdout
    identity = {"path": str(path), "version": _version_string(output),
                "configuration_sha256": configuration_sha256(_parse_configuration(output))}
    if key is not None:
        _identity_cache[key] = identity
    return dict(identity)


def clear_capabilities_cache():
    """Forget the in-process copies (the on-disk cache stays; capabilities(refresh=True) rewrites it)."""
    _capabilities_cache.clear()


# --- runner and -progress parsing --------------------------------------------

# Every child started by run() (and encode.write_par2) while it runs, so a SIGTERM to vidfunnel alone,
# which unlike Ctrl+C in a terminal does not reach the children, can pass it on (terminate_children).
# Reentrant, which costs nothing; the SIGTERM handler itself no longer takes it (see vidfunnel.cli._on_sigterm).
_children = set()
_children_lock = threading.RLock()


def register_child(process):
    """Note a started child (a subprocess.Popen) until forget_child(); see terminate_children.

    A child registered after the interrupt event was set (a SIGTERM that
    arrived between its Popen and this call) is sent SIGTERM at once, since
    terminate_children() ran before it was in the set.
    """
    with _children_lock:
        _children.add(process)
    if interrupted.is_set():
        try:
            process.send_signal(signal.SIGTERM)
        except OSError:
            pass


def forget_child(process):
    with _children_lock:
        _children.discard(process)


def terminate_children(signum=signal.SIGTERM):
    """Send `signum` to every registered child that is still running; returns how many were signalled."""
    with _children_lock:
        running = list(_children)
    sent = 0
    for process in running:
        try:
            process.send_signal(signum)  # a no-op for a child already reaped
        except OSError:
            continue
        sent += 1
    return sent



def _number(text, kind):
    if text is None:
        return None
    text = text.strip()
    if text in ("", "N/A"):
        return None
    try:
        return kind(text)
    except ValueError:
        return None


def _clock_to_seconds(text):
    """'01:02:03.500000' -> 3723.5, or None."""
    match = re.match(r"^(-?)(\d+):(\d+):(\d+(?:\.\d+)?)$", text.strip())
    if match is None:
        return None
    seconds = int(match.group(2)) * 3600 + int(match.group(3)) * 60 + float(match.group(4))
    return -seconds if match.group(1) else seconds


def parse_progress_block(lines):
    """One dict from the key=value lines of one -progress block.

    Values stay strings except: out_time_s (float seconds, from out_time_us,
    else out_time_ms, which ffmpeg also writes in microseconds, else the
    out_time clock; None when unknown or negative, as at the very start),
    frame (int), fps (float), speed (float, '8.1x' -> 8.1) and total_size
    (int); 'N/A' becomes None for those.
    """
    block = {}
    for line in lines:
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        block[key.strip()] = value.strip()
    seconds = None
    for key in ("out_time_us", "out_time_ms"):
        micro = _number(block.get(key), int)
        if micro is not None:
            seconds = micro / 1000000.0
            break
    if seconds is None and block.get("out_time"):
        seconds = _clock_to_seconds(block["out_time"])
    if seconds is not None and seconds < 0:
        seconds = None
    block["out_time_s"] = seconds
    for key, kind in (("frame", int), ("fps", float), ("total_size", int)):
        if key in block:
            block[key] = _number(block[key], kind)
    if "speed" in block:
        block["speed"] = _number(block["speed"].rstrip("x"), float)
    return block


def _error_message(argv, returncode, stderr):
    tail = [line for line in stderr.splitlines() if line.strip()][-ERROR_TAIL_LINES:]
    message = "'{}' exited with status {}".format(os.path.basename(argv[0]), returncode)
    if tail:
        message += ":\n  " + "\n  ".join(tail)
    return message


def _collect(process, progress):
    """(stdout, stderr) of a started child, feeding -progress blocks to `progress` when given (see run).

    On any exception (Ctrl+C, SIGTERM's Terminated, a failing callback) the
    child is killed and reaped before the exception propagates, so the run
    never exits while its child still runs.
    """
    if progress is None:
        try:
            return process.communicate()
        except BaseException:
            process.kill()
            process.wait()
            for stream in (process.stdout, process.stderr):
                stream.close()
            raise
    stderr_parts = []
    reader = threading.Thread(target=lambda: stderr_parts.append(process.stderr.read()), daemon=True)
    reader.start()
    stdout_lines = []
    block_lines = []
    try:
        for line in process.stdout:
            stdout_lines.append(line)
            block_lines.append(line)
            if line.startswith("progress="):
                block = parse_progress_block(block_lines)
                block_lines = []
                progress(block)
        process.wait()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        reader.join()
        process.stdout.close()
        process.stderr.close()
    stdout = "".join(stdout_lines)
    stderr = "".join(stderr_parts)
    return stdout, stderr


def run(argv, progress=None, check=True):
    """Run argv (a list) and return CompletedRun(argv, returncode, stdout, stderr).

    With a `progress` callable the caller has put '-nostats -progress pipe:1'
    in argv; stdout is read line by line while ffmpeg runs and every finished
    block (ending at its 'progress=' line) goes to progress(block) as parsed
    by parse_progress_block. stderr is collected whole on a reader thread.
    check=True raises FfmpegError on a non-zero status. A missing executable
    raises ToolNotFound. If the callback raises (or Ctrl+C arrives), ffmpeg is
    killed before the exception propagates. The child is in the registry
    of terminate_children() while it runs.

    While vidfunnel.interrupt.interrupted is set (Ctrl+C during a --jobs
    pool), no process is started and a child that exits non-zero is not
    reported: both raise Interrupted, whatever `check` says (see
    vidfunnel.interrupt.child_interrupted).
    """
    argv = [os.fspath(part) for part in argv]
    check_interrupted()
    popen_options = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        process = subprocess.Popen(argv, **popen_options)
    except FileNotFoundError:
        raise ToolNotFound("'{}' does not exist".format(argv[0]))
    except PermissionError:
        raise ToolNotFound("'{}' is not executable".format(argv[0]))
    register_child(process)
    try:
        stdout, stderr = _collect(process, progress)
    finally:
        forget_child(process)

    if child_interrupted(process.returncode):
        raise Interrupted("'{}' was stopped by the interrupt (status {})".format(
            os.path.basename(argv[0]), process.returncode))
    result = CompletedRun(argv, process.returncode, stdout, stderr)
    if check and process.returncode != 0:
        raise FfmpegError(_error_message(argv, process.returncode, stderr), argv, process.returncode, stderr)
    return result


# --- ffprobe ------------------------------------------------------------------


def _input_argument(path):
    """Absolute path string, so names beginning with '-' or holding 'x:' are never read as options
    or protocols."""
    return os.path.abspath(os.fspath(path))


def _ffprobe_json_run(argv, path):
    completed = run(argv, check=False)
    if completed.returncode != 0:
        raise FfmpegError(
            "ffprobe could not read '{}': {}".format(
                path, _error_message(argv, completed.returncode, completed.stderr)
            ),
            argv,
            completed.returncode,
            completed.stderr,
        )
    try:
        return json.loads(completed.stdout)
    except ValueError as error:
        raise FfmpegError(
            "ffprobe printed invalid JSON for '{}': {}".format(path, error),
            argv,
            completed.returncode,
            completed.stderr,
        )


def ffprobe_json(tools, path, extra_args=()):
    """Parsed `ffprobe -show_format -show_streams -show_chapters` JSON for path."""
    argv = [tools.ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", "-show_chapters"]
    argv.extend(extra_args)
    argv.append(_input_argument(path))
    return _ffprobe_json_run(argv, path)


def ffprobe_packets(tools, path, stream_spec, entries):
    """List of packet dicts (only `entries`, e.g. 'pts_time,flags') for the selected stream."""
    argv = [
        tools.ffprobe,
        "-v",
        "error",
        "-select_streams",
        str(stream_spec),
        "-show_entries",
        "packet=" + entries,
        "-print_format",
        "json",
        _input_argument(path),
    ]
    return _ffprobe_json_run(argv, path).get("packets", [])


# --- decode check (HANDOFF 4.1) ----------------------------------------------

_POINTER_TAG = re.compile(r"\[([^\[\]]*?) @ 0x[0-9a-fA-F]+\]")
_LEADING_TAGS = re.compile(r"^((?:\[[^\[\]]*\]\s*)*)(.*)$")
_HEX = re.compile(r"0x[0-9a-fA-F]+")
_DIGITS = re.compile(r"(?<![A-Za-z0-9])\d+")
_REPEATED = re.compile(r"^(?:\[[^\[\]]*\]\s*)*Last message repeated (\d+) times?\s*$")


def normalise_message(line):
    """A message class: '[h264 @ 0x55a] error while decoding MB 32 20' -> '[h264] error while decoding MB # #'.

    The ' @ 0x...' pointer is dropped from the leading tags, hex values and
    runs of digits become '#' (digits right after a letter stay, so codec
    names such as 'h264' and 'mpeg4' survive), whitespace is collapsed.
    """
    line = _POINTER_TAG.sub(r"[\1]", line.strip())
    match = _LEADING_TAGS.match(line)
    tags, body = match.group(1), match.group(2)
    tags = _DIGITS.sub("#", tags)
    body = _DIGITS.sub("#", _HEX.sub("#", body))
    return " ".join((tags + body).split())


def classify_messages(stderr):
    """dict message class -> count from ffmpeg stderr; 'Last message repeated N times' adds N."""
    classes = {}
    previous = None
    for raw in re.split(r"[\r\n]+", stderr):
        if not raw.strip():
            continue
        repeated = _REPEATED.match(raw.strip())
        if repeated and previous is not None:
            classes[previous] += int(repeated.group(1))
            continue
        message = normalise_message(raw)
        classes[message] = classes.get(message, 0) + 1
        previous = message
    return classes


def null_decode(tools, path, progress=None):
    """Decode the whole file to the null muxer; DecodeResult(returncode, message_classes, stderr).

    `ffmpeg -v warning -nostats -progress pipe:1 -i SRC -map 0:v? -map 0:a?
    -f null -`, without -xerror (HANDOFF 4.1). Every video and audio stream
    is decoded, not only ffmpeg's default pick of one each, so damage in a
    second audio track is seen. A non-zero status is returned, not raised:
    the caller decides what it means. `progress` receives the -progress blocks.
    """
    argv = [
        tools.ffmpeg,
        "-v",
        "warning",
        "-nostats",
        "-progress",
        "pipe:1",
        "-i",
        _input_argument(path),
        "-map",
        "0:v?",
        "-map",
        "0:a?",
        "-f",
        "null",
        "-",
    ]
    completed = run(argv, progress=progress if progress is not None else (lambda block: None), check=False)
    return DecodeResult(completed.returncode, classify_messages(completed.stderr), completed.stderr)

# End of File #
