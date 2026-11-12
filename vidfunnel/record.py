"""Per-folder JSON records, fingerprints and the folder lock (HANDOFF.md section 6.7).

Each processed folder holds a hidden '.vidfunnel/' directory with one JSON record
per source file, named after the full source file name including its extension
('movie.avi.json'), plus 'folder.json' and, while a run is active, 'lock'.
Records are the authority; every write is atomic (temp file, then os.replace).
"""

VERSION = "20261111"

import contextlib
import datetime
import errno
import hashlib
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

try:
    import fcntl
except ImportError:  # not on Windows, which vidfunnel does not support; takeovers are then unserialised
    fcntl = None     # and no lock carries a flock

import vidfunnel
from vidfunnel.interrupt import check as check_interrupted
from vidfunnel.interrupt import drain_for_lost_lock
from vidfunnel.progress import _write as _write_line

RECORD_DIR = ".vidfunnel"
LOCK_NAME = "lock"
FOLDER_FILE = "folder.json"
RECORD_SUFFIX = ".json"
STATUSES = ("planned", "converted", "verified", "quarantined", "failed", "retired", "source-changed")
STAGES = ("probe", "analyze", "plan", "convert", "refit", "verify", "retire")

# HANDOFF section 5 bands; shared by verify, report and retire.
BAND_OK = 90
BAND_REVIEW = 60


def band(score):
    """'ok' at >= 90, 'review' at 60-89, 'hold' below 60; None when there is no score."""
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    if score >= BAND_OK:
        return "ok"
    if score >= BAND_REVIEW:
        return "review"
    return "hold"

MIB = 1024 * 1024
FINGERPRINT_CHUNK = MIB              # first and last MiB
HASH_BLOCK = MIB                     # read size for full hashes
PROGRESS_EVERY = 8 * MIB             # sha256_file reports this often
LOCK_STALE_SECONDS = 24 * 60 * 60

# The lock heartbeat (see FolderLock): a holder advances its lock file's mtime this often, and a lock
# whose last beat is older than HEARTBEAT_STALE_SECONDS on the share's clock is taken for dead.
HEARTBEAT_SECONDS = 300
HEARTBEAT_STALE_SECONDS = 3600
HEARTBEAT_POLL_SECONDS = 1.0         # the heartbeat thread looks for released locks and wake-ups this often
# A poll whose wall clock moved this much more than its monotonic clock follows a suspend: beat every lock now.
# Well above the poll interval and scheduling delays, well below HEARTBEAT_STALE_SECONDS.
HEARTBEAT_WAKE_GAP_SECONDS = 30
# A lock's last beat may sit this far ahead of the share's clock (attribute rounding) and still be judged by it;
# further ahead, the share's clock cannot judge that lock and the 24 h rule applies.
HEARTBEAT_AHEAD_SECONDS = 5
HEARTBEAT_FIELD = "heartbeat"        # the lock JSON field that says its holder beats
PROBE_PREFIX = "lock.probe."         # probe files that read the share's clock: 'lock.probe.<uuid4 hex>'

# A takeover of a lock judged dead (see FolderLock._replace). Its marker is created with O_EXCL, so one run at a
# time may replace the lock; a marker older than TAKEOVER_MARKER_STALE_SECONDS on the share's clock was left by a
# run that stopped mid-takeover and is moved aside. A taker writes its lock to 'lock.new.<uuid4 hex>' and renames
# it over the lock path; a stale marker is moved to 'lock.aside.<uuid4 hex>' and removed. Either can be left
# behind by a run that died; a later takeover removes those older than TAKEOVER_MARKER_STALE_SECONDS. That sweep
# takes only names of exactly this form (the prefix and 32 lowercase hex digits, see _is_takeover_leftover), so a
# record of a source named 'lock.new.trailer.mkv' ('lock.new.trailer.mkv.json') or a user's file is never swept;
# and no such name ends in '.json', so none is ever read as a record.
TAKEOVER_MARKER = "lock.taking"
TAKEOVER_MARKER_STALE_SECONDS = 300
NEW_LOCK_PREFIX = "lock.new."
MARKER_ASIDE_PREFIX = "lock.aside."
TAKEOVER_LEFTOVER_PREFIXES = (NEW_LOCK_PREFIX, MARKER_ASIDE_PREFIX)
_UUID_HEX_DIGITS = frozenset("0123456789abcdef")


def _is_takeover_leftover(name):
    """True when name is a takeover's own temporary file: a leftover prefix and a uuid4().hex, nothing more."""
    for prefix in TAKEOVER_LEFTOVER_PREFIXES:
        if name.startswith(prefix):
            rest = name[len(prefix):]
            return len(rest) == 32 and set(rest) <= _UUID_HEX_DIGITS
    return False


class RecordError(Exception):
    """A record or folder file exists but cannot be read or parsed."""


class LockLost(Exception):
    """This process's lock of a folder was taken over by another run, or could not be checked (see ensure_lock).

    Raised before anything more of an item is written in that folder; the
    stages count the item as skipped with this message.
    """


class LockHeld(Exception):
    """Another run holds the folder lock and it is not taken for dead (see FolderLock).

    beat_age_seconds is set when the lock was judged by its heartbeat: the
    seconds since its holder's last beat on the share's clock (the lock is
    taken over once that passes HEARTBEAT_STALE_SECONDS); None otherwise.
    """

    def __init__(self, message, host=None, pid=None, age_seconds=None, beat_age_seconds=None):
        Exception.__init__(self, message)
        self.host = host
        self.pid = pid
        self.age_seconds = age_seconds
        self.beat_age_seconds = beat_age_seconds


# ---------------------------------------------------------------- time


def now_iso():
    """Current UTC time as 'YYYY-MM-DDTHH:MM:SSZ'."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    """Parse a now_iso() string back to an aware datetime; raises ValueError."""
    parsed = datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    return parsed.replace(tzinfo=datetime.timezone.utc)


# ---------------------------------------------------------------- paths


def record_dir(folder):
    """The '.vidfunnel' directory of a folder."""
    return Path(folder) / RECORD_DIR


def record_path(source):
    """Record file of a source: '<folder>/.vidfunnel/<full source name>.json'."""
    source = Path(source)
    return record_dir(source.parent) / (source.name + RECORD_SUFFIX)


# ---------------------------------------------------------------- permissions


def take_folder_mode(target, folder, directory=False):
    """Add to `target` the permissions the library folder `folder` grants; best effort, never raises.

    `target` is a path or an open descriptor of something this run has just
    created in the library. A file gains the bits of the folder's mode
    without the execute, setuid, setgid and sticky bits (folder 0775 or
    2775: rw for user, group and others' r); a directory gains the folder's
    mode with setgid but without the sticky and setuid bits (a sticky
    '.vidfunnel' would stop one account renaming over another account's
    record or lock). Bits are only ever added, never
    removed: on a network mount the folder's mode as this host sees it may
    be the mount's (CIFS dir_mode, often 0755), not the server's, while the
    server gives the new file its own mode (an inherited ACL), which must
    not be narrowed. When nothing would be added no chmod is made. Hosts
    that mount the share as another account of the same group can then
    read and rewrite what this one writes. A folder or target that cannot
    be stat'ed or a chmod that fails (not the owner, a file system or CIFS
    server that decides modes itself) leaves the mode as it is, silently.
    Never chowns.
    """
    try:
        granted = stat.S_IMODE(os.stat(str(folder)).st_mode)
        if directory:
            granted &= ~(stat.S_ISVTX | stat.S_ISUID)  # a sticky '.vidfunnel' would stop renames over others' files
        else:
            granted &= 0o666
        if isinstance(target, int):
            current = stat.S_IMODE(os.fstat(target).st_mode)
        else:
            current = stat.S_IMODE(os.stat(str(target)).st_mode)
        mode = current | granted
        if mode == current:
            return
        if isinstance(target, int):
            os.fchmod(target, mode)
        else:
            os.chmod(str(target), mode)
    except OSError:
        pass


def make_record_dir(folder):
    """Create '<folder>/.vidfunnel' when it is missing and widen it to the folder's mode; return its path.

    The mode is widened by take_folder_mode. An existing directory is left as it is.
    """
    directory = record_dir(folder)
    try:
        directory.mkdir(parents=True)
    except FileExistsError:
        if not directory.is_dir():
            raise
        return directory
    take_folder_mode(directory, folder, directory=True)
    return directory


# ---------------------------------------------------------------- JSON I/O


def _dump_json(data):
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _write_json_atomic(path, data):
    """Write data as JSON to path ('<folder>/.vidfunnel/<name>') through a temp file in the same directory.

    The temp file gains the mode of <folder> (take_folder_mode) before it is
    renamed into place, and so does '.vidfunnel' when it is created here.
    """
    path = Path(path)
    library = path.parent.parent
    make_record_dir(library)
    text = _dump_json(data)
    handle, temp_name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        take_folder_mode(handle, library)
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, str(path))
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _read_json(path):
    """Parsed JSON of path, None when it does not exist, RecordError when unreadable.

    JSON nested too deeply for the parser (RecursionError) is unreadable too.
    A permission error adds a clause on the likely cause and its fix.
    """
    path = Path(path)
    try:
        with open(str(path), "r", encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        if isinstance(error, OSError) and error.errno in (errno.EACCES, errno.EPERM):
            raise RecordError("cannot read '{}': {}; it probably belongs to another account and has owner-only "
                              "permissions, which the owner can fix with chmod on the server (chmod g+rw)".format(
                                  path, error))
        raise RecordError("cannot read '{}': {}".format(path, error))
    except RecursionError:
        raise RecordError("cannot read '{}': its JSON is nested too deeply".format(path)) from None


# ---------------------------------------------------------------- records


def load_record(source):
    """The record of a source, or None when it has none. RecordError when it is unreadable."""
    path = record_path(source)
    data = _read_json(path)
    if data is not None and not isinstance(data, dict):
        raise RecordError("record '{}' is not a JSON object".format(path))
    return data


def save_record(source, record):
    """Write the record of a source atomically, creating '.vidfunnel/' when needed.

    LockLost, and nothing written, when this process held the folder's lock
    and another run has taken it over, or it cannot be checked (ensure_lock).
    """
    ensure_lock(Path(source).parent)
    _write_json_atomic(record_path(source), record)


def iter_records(root, on_error=None):
    """Yield (folder, record) for every record under root, sorted by path.

    on_error: None raises RecordError on an unreadable record; otherwise it is
    called as on_error(path, error) and the walk continues.
    """
    root = Path(root)
    for current, dir_names, _file_names in os.walk(str(root)):
        dir_names.sort()
        if RECORD_DIR not in dir_names:
            continue
        dir_names.remove(RECORD_DIR)
        folder = Path(current)
        records_location = folder / RECORD_DIR
        for entry in sorted(records_location.iterdir()):
            if entry.name == FOLDER_FILE or not entry.name.endswith(RECORD_SUFFIX):
                continue
            if entry.name.startswith(".") or not entry.is_file():
                continue
            try:
                data = _read_json(entry)
                if not isinstance(data, dict):
                    raise RecordError("record '{}' is not a JSON object".format(entry))
            except RecordError as error:
                if on_error is None:
                    raise
                on_error(entry, error)
                continue
            yield folder, data


def new_record(source, folder=None):
    """A fresh record for a source: new uuid4 id, cheap fingerprint, status None."""
    source = Path(source)
    if folder is None:
        folder = source.parent
    fingerprint = cheap_fingerprint(source)
    return {
        "id": str(uuid.uuid4()),
        "tool": "vidfunnel",
        "tool_version": vidfunnel.VERSION,
        "source": {
            "name": source.name,
            "folder": str(Path(folder).absolute()),
            "size": fingerprint["size"],
            "fingerprint": fingerprint,
            "sha256": None,
        },
        "status": None,
        "status_at": None,
        "created": now_iso(),
        "flags": [],
        "penalties": [],
        "score": None,
        "action": None,
        "quarantine_reason": None,
        "output": {
            "name": None,
            "interim_name": None,
            "sha256": None,
            "fingerprint": None,
            "size": None,
            "sidecars": [],
        },
        "stages": {},
        "history": [],
    }


def add_stage(record, stage, data):
    """Add a stage section stamped with 'at'; ValueError when it already exists."""
    if stage not in STAGES:
        raise ValueError("unknown stage '{}'; expected one of {}".format(stage, ", ".join(STAGES)))
    stages = record.setdefault("stages", {})
    if stage in stages:
        raise ValueError("stage '{}' already recorded at {}; records are append-only per stage".format(
            stage, stages[stage].get("at")))
    section = dict(data)
    section["at"] = now_iso()
    stages[stage] = section


def add_flag(record, flag):
    """Append a flag unless it is already present."""
    flags = record.setdefault("flags", [])
    if flag not in flags:
        flags.append(flag)


def add_penalty(record, code, points, reason):
    """Append a penalty entry."""
    record.setdefault("penalties", []).append({"code": code, "points": points, "reason": reason})


def set_status(record, status):
    """Set the status (one of STATUSES) and its timestamp."""
    if status not in STATUSES:
        raise ValueError("unknown status '{}'; expected one of {}".format(status, ", ".join(STATUSES)))
    record["status"] = status
    record["status_at"] = now_iso()


# ---------------------------------------------------------------- fingerprints and hashes


def cheap_fingerprint(path):
    """Size, mtime_ns and SHA-256 of the first and last MiB (whole file once when < 2 MiB)."""
    path = Path(path)
    with open(str(path), "rb") as stream:
        status = os.fstat(stream.fileno())
        size = status.st_size
        if size < 2 * FINGERPRINT_CHUNK:
            whole = hashlib.sha256(stream.read()).hexdigest()
            head_hex = whole
            tail_hex = whole
        else:
            head_hex = hashlib.sha256(stream.read(FINGERPRINT_CHUNK)).hexdigest()
            stream.seek(size - FINGERPRINT_CHUNK)
            tail_hex = hashlib.sha256(stream.read(FINGERPRINT_CHUNK)).hexdigest()
    return {
        "size": size,
        "mtime_ns": status.st_mtime_ns,
        "head_sha256": head_hex,
        "tail_sha256": tail_hex,
    }


# What decides whether a file is unchanged: its size and the first and last MiB (CONTENT_KEYS),
# then its modification time (check_source). The same file seen through another mount, or
# restored, can show its time at a coarser step; such a time is accepted without a read only when
# it is the other one truncated to one of MTIME_STEPS_NS. Any other difference is settled by
# reading the whole file and comparing its SHA-256 with the record's.
CONTENT_KEYS = ("size", "head_sha256", "tail_sha256")
_CONTENT_WORDS = {"head_sha256": "first MiB", "tail_sha256": "last MiB"}
MTIME_STEPS_NS = (100, 1000, 1000 * 1000, 10 * 1000 * 1000, 1000 * 1000 * 1000, 2 * 1000 * 1000 * 1000)
FULL_HASH_PROGRESS_BYTES = 32 * MIB      # a full read shows live progress above this size


def fingerprint_matches(first, second):
    """True when both fingerprints are present and agree on size and the first and last MiB; mtime is not compared."""
    return not fingerprint_differences(first, second)


def fingerprint_identical(first, second):
    """True when both fingerprints agree on every field, mtime included: one process looking at one file twice."""
    if not fingerprint_matches(first, second):
        return False
    return first.get("mtime_ns") is not None and first.get("mtime_ns") == second.get("mtime_ns")


def fingerprint_differences(recorded, seen):
    """Words naming each content field in which `seen` differs from `recorded`; [] when fingerprint_matches."""
    if not isinstance(recorded, dict) or not recorded:
        return ["no fingerprint recorded"]
    if not isinstance(seen, dict) or not seen:
        return ["no fingerprint taken"]
    differences = []
    for key in CONTENT_KEYS:
        if recorded.get(key) is not None and recorded.get(key) == seen.get(key):
            continue
        if key == "size":
            differences.append("size {} bytes recorded, {} now".format(recorded.get(key), seen.get(key)))
        else:
            differences.append("{} differs".format(_CONTENT_WORDS.get(key, key)))
    return differences


def mtime_difference(recorded, seen):
    """None when both fingerprints carry the same integer mtime_ns; else {'recorded': ..., 'seen': ...}.

    A recorded value that is missing or not an integer counts as different
    (its 'recorded' is then whatever the record holds, None when absent).
    """
    recorded_ns = recorded.get("mtime_ns") if isinstance(recorded, dict) else None
    seen_ns = seen.get("mtime_ns") if isinstance(seen, dict) else None
    if _whole_number(recorded_ns) and _whole_number(seen_ns) and recorded_ns == seen_ns:
        return None
    return {"recorded": recorded_ns, "seen": seen_ns}


def mtime_is_coarser_view(first_ns, second_ns):
    """True when two mtimes are equal or one is the other truncated to a step of MTIME_STEPS_NS.

    Truncation only (coarse == fine - fine % step), in either direction; no
    rounding and no tolerance. False when either is not an integer.
    """
    if not _whole_number(first_ns) or not _whole_number(second_ns):
        return False
    if first_ns == second_ns:
        return True
    for step in MTIME_STEPS_NS:
        if first_ns == second_ns - second_ns % step or second_ns == first_ns - first_ns % step:
            return True
    return False


def fingerprint_same_file(recorded, seen):
    """fingerprint_matches, and the mtimes are equal or one is a coarser view of the other (mtime_is_coarser_view)."""
    return fingerprint_matches(recorded, seen) and mtime_is_coarser_view(recorded.get("mtime_ns"),
                                                                          seen.get("mtime_ns"))


def _whole_number(value):
    return isinstance(value, int) and not isinstance(value, bool)


def full_sha256(paths, label, stream=None, quiet=False, progress=None):
    """SHA-256 of the files concatenated in order (one file: its own), as plan hashes a source.

    Announces the read on `stream` unless quiet, and shows a live progress
    line above FULL_HASH_PROGRESS_BYTES; `progress`, when given, is a
    Progress-like object that receives the byte counts instead.
    """
    from vidfunnel.progress import Progress, format_bytes, say
    total = sum(os.path.getsize(str(path)) for path in paths)
    if not quiet:
        say("  reading '{}' in full ({}): its modification time is neither the recorded one nor a coarser view "
            "of it; comparing its SHA-256 with the record's".format(label, format_bytes(total)), stream)
    digest = hashlib.sha256()
    live = progress
    if live is None and total > FULL_HASH_PROGRESS_BYTES:
        live = Progress("sha256", total=total, unit="B", stream=stream, quiet=quiet)
    done = 0
    try:
        for path in paths:
            with open(str(path), "rb") as handle:
                while True:
                    check_interrupted()
                    block = handle.read(HASH_BLOCK)
                    if not block:
                        break
                    digest.update(block)
                    done += len(block)
                    if live is not None:
                        live.update(done)
    finally:
        if live is not None and progress is None:
            live.finish()
    return digest.hexdigest()


class SourceCheck(object):
    """What check_source found: reason (None when unchanged), notes, sha256_checked, prints, sha256, unreadable."""

    def __init__(self, reason, notes, sha256_checked, prints, sha256=None, unreadable=False):
        self.reason = reason
        self.unreadable = unreadable
        self.notes = notes
        self.sha256_checked = sha256_checked
        self.prints = prints
        self.sha256 = sha256

    def stage_notes(self):
        """{'source_mtime_differs': [...], 'source_sha256_checked': bool} when a time differs, else {}."""
        if not self.notes:
            return {}
        return {"source_mtime_differs": self.notes, "source_sha256_checked": self.sha256_checked}


# Full hashes read in this process, so one run that checks a source twice (retire's confirmation
# list and the retire itself) reads it once: {paths: (identities before and after the read, digest)}.
# An identity holds the fingerprint and the inode's device, number and ctime, so a file written or
# touched since is read again.
_full_hash_memo = {}
_full_hash_memo_lock = threading.Lock()


def _identities(paths, prints):
    found = []
    for path, fingerprint in zip(paths, prints):
        status = os.stat(str(path))
        found.append((status.st_dev, status.st_ino, status.st_ctime_ns, fingerprint.get("size"),
                      fingerprint.get("mtime_ns"), fingerprint.get("head_sha256"), fingerprint.get("tail_sha256")))
    return tuple(found)


def check_source(files, recorded_sha256, label, stream=None, quiet=False, progress=None):
    """Compare a source's files with their recorded fingerprints; a SourceCheck.

    files: [(path, recorded fingerprint)] in source order (a VOB group's
    members; one entry otherwise). A file whose size or first or last MiB
    differs is changed. When every time is the recorded one or a coarser view
    of it (mtime_is_coarser_view), the source is unchanged without a read.
    Otherwise the files are read once in full, concatenated in order
    (full_sha256), and the source is unchanged only when that SHA-256 equals
    recorded_sha256; without one it is changed. notes lists {'name',
    'recorded', 'seen'} for each file whose time is not the recorded one.
    """
    prints = []
    notes = []
    settle = False
    for path, recorded in files:
        path = Path(path)
        try:
            current = cheap_fingerprint(path)
        except OSError as error:
            return SourceCheck("'{}' cannot be read: {}".format(path.name, error), [], False, prints, unreadable=True)
        differences = fingerprint_differences(recorded, current)
        if differences:
            return SourceCheck("'{}' differs from the fingerprint taken at plan ({})".format(
                path.name, ", ".join(differences)), [], False, prints)
        prints.append(current)
        moved = mtime_difference(recorded, current)
        if moved is not None:
            notes.append(dict(name=path.name, **moved))
            if not mtime_is_coarser_view(moved["recorded"], moved["seen"]):
                settle = True
    if not settle:
        return SourceCheck(None, notes, False, prints)
    if not isinstance(recorded_sha256, str) or not recorded_sha256:
        return SourceCheck("the modification time of '{}' is neither the recorded one nor a coarser view of it, "
                           "and the record has no source SHA-256 to compare its content with".format(label),
                           notes, False, prints)
    paths = tuple(os.path.abspath(str(path)) for path, _recorded in files)
    try:
        before = _identities(paths, prints)
        with _full_hash_memo_lock:
            known = _full_hash_memo.get(paths)
        if known is not None and known[0] == before:
            digest = known[1]
        else:
            digest = full_sha256(paths, label, stream, quiet, progress)
            after = _identities(paths, [cheap_fingerprint(path) for path in paths])
            if after != before:
                return SourceCheck("'{}' changed while it was being read".format(label), notes, True, prints)
            with _full_hash_memo_lock:
                _full_hash_memo[paths] = (before, digest)
    except OSError as error:
        return SourceCheck("'{}' cannot be read: {}".format(label, error), notes, False, prints, unreadable=True)
    if digest.lower() != recorded_sha256.lower():
        return SourceCheck("SHA-256 of '{}' is {}, the record says {}".format(label, digest, recorded_sha256),
                           notes, True, prints, digest)
    return SourceCheck(None, notes, True, prints, digest)


def sha256_file(path, progress=None):
    """Full SHA-256 hex of a file; progress.update(bytes_done) every 8 MiB and at the end."""
    digest = hashlib.sha256()
    done = 0
    next_report = PROGRESS_EVERY
    with open(str(path), "rb") as stream:
        while True:
            check_interrupted()
            block = stream.read(HASH_BLOCK)
            if not block:
                break
            digest.update(block)
            done += len(block)
            if progress is not None and done >= next_report:
                progress.update(done)
                next_report = done + PROGRESS_EVERY
    if progress is not None:
        progress.update(done)
    return digest.hexdigest()


# ---------------------------------------------------------------- folder.json


def load_folder(folder):
    """Contents of '.vidfunnel/folder.json', or an empty dict when there is none."""
    path = record_dir(folder) / FOLDER_FILE
    data = _read_json(path)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise RecordError("'{}' is not a JSON object".format(path))
    return data


def save_folder(folder, data):
    """Write '.vidfunnel/folder.json' atomically; LockLost, and nothing written, as for save_record."""
    ensure_lock(folder)
    _write_json_atomic(record_dir(folder) / FOLDER_FILE, data)


# ---------------------------------------------------------------- lock


def _warn(message):
    """Write a line to stderr; never raises, also on the heartbeat thread.

    A reader that is gone (EIO, EPIPE) points stderr at /dev/null, as for
    every other line (vidfunnel.progress._write); a closed stream (ValueError)
    or none at all (AttributeError: sys.stderr is None) drops the line.
    """
    try:
        _write_line(sys.stderr, message + "\n")
    except (OSError, ValueError, AttributeError):
        pass


# Files a container runtime leaves in the container's root: podman's and Docker's.
CONTAINER_MARKERS = ("/run/.containerenv", "/.dockerenv")
TAKEOVER_FILE = "lock.takeover"  # the regular file the takeover flock is taken on, beside the lock


def in_container():
    """True when this process runs inside a podman or Docker container (one of CONTAINER_MARKERS exists)."""
    return any(os.path.exists(marker) for marker in CONTAINER_MARKERS)


MACHINE_ID_FILES = ("/etc/machine-id", "/proc/sys/kernel/random/boot_id")
MACHINE_ID_SOURCES = ("machine-id", "boot-id")  # the lock's machine_source for each of MACHINE_ID_FILES


def machine_identity():
    """(identity of this machine, its source) for the lock file; (None, None) when none can be found.

    /etc/machine-id (source 'machine-id'), else the kernel boot id (Linux;
    'boot-id'), else the hardware address uuid.getnode() reports (macOS has
    neither file; 'hardware'), unless that is the random value getnode falls
    back to (multicast bit set). Two machines on one share may carry the
    same host name (fresh installs, images); a pid is judged alive or dead
    only when the lock was written on this very machine, which the host
    name alone cannot tell. Only a 'machine-id' identity outlives a reboot
    while the boot id changes, which the reboot rule of FolderLock needs.
    """
    for path, source in zip(MACHINE_ID_FILES, MACHINE_ID_SOURCES):
        try:
            with open(path, encoding="ascii", errors="replace") as handle:
                value = handle.read().strip()
        except OSError:
            continue
        if value:
            return value, source
    node = uuid.getnode()
    if (node >> 40) & 1:  # a random 48-bit value, not a hardware address
        return None, None
    return "node-{:012x}".format(node), "hardware"


def machine_id():
    """An identity of this machine for the lock file, or None when none can be found (see machine_identity)."""
    return machine_identity()[0]


BOOT_ID_FILE = "/proc/sys/kernel/random/boot_id"


def boot_id():
    """The running kernel's boot id (Linux), or None when it cannot be read (macOS has no such file).

    Every process on one kernel reads the same value, inside a container or
    not, and the value changes at every boot. Two lock files with the same
    boot id were written on this kernel since it booted, so a flock() on one
    of them is the same lock for both runs (see FolderLock).
    """
    try:
        with open(BOOT_ID_FILE, encoding="ascii", errors="replace") as handle:
            value = handle.read().strip()
    except OSError:
        return None
    return value or None


# Descriptor -> (st_dev, st_ino) of every lock file this process holds; see held_lock_fds().
_held_locks = {}
_held_locks_guard = threading.Lock()


@contextlib.contextmanager
def held_lock_fds():
    """Yield the sorted descriptors of the lock files this process holds, for Popen(pass_fds=...).

    vidfunnel.ffmpeg.run and encode.write_par2 start every child inside this
    block, so each ffmpeg, ffprobe and par2 inherits every folder lock's
    descriptor, and with it the lock's flock, which belongs to the open file
    and not to a process: the lock is free when the last process holding the
    descriptor exits, so an encoder orphaned by a killed vidfunnel keeps its
    folder locked until it ends. With --jobs a child may also inherit the
    lock of a folder it does not work on; that lock is held by this process
    too, so it is freed no later than it would be anyway, unless the child
    outlives this process. The guard is held until the block ends, and
    FolderLock.release() closes a descriptor only under it, so no descriptor
    is closed (or its number reused) between this snapshot and the child's
    start, which would make Popen fail with EBADF.
    """
    with _held_locks_guard:
        yield tuple(sorted(_held_locks))


def _held_here(device, inode):
    """True when this process holds the lock file with this st_dev and st_ino."""
    with _held_locks_guard:
        return (device, inode) in _held_locks.values()


class _Beat:
    """One held lock in the heartbeat registry; its state fields change only under _held_locks_guard."""

    def __init__(self, handle, path, folder, status, first_byte):
        self.handle = handle
        self.path = str(path)
        self.folder = folder
        self.identity = (status.st_dev, status.st_ino)
        self.first_byte = first_byte          # the byte the holder wrote at offset 0, rewritten by each beat
        self.due = time.monotonic() + HEARTBEAT_SECONDS
        self.beating = False                  # the heartbeat thread is using the descriptor outside the guard
        self.released = False                 # release() has begun: no further beat, no lost-lock verdict
        self.handover = False                 # release() left the descriptor open for the beating thread to close
        self.lost = False                     # the lock path names another file: another run took the folder
        self.failure_reported = False
        self.crash_reported = False
        self.count = 0                        # beats written


_beats = []          # the _Beat of every lock this process holds and still beats; under _held_locks_guard
_folder_beats = {}   # os.path.abspath(folder) -> its _Beat while this process holds that lock, lost or not; same guard
_beat_thread = None  # the heartbeat thread while one runs; under _held_locks_guard


def _start_beating(beat):
    """Add a lock to the heartbeat registry and start the thread if none runs; the caller holds the guard."""
    global _beat_thread
    _beats.append(beat)
    if _beat_thread is None:
        try:
            thread = threading.Thread(target=_heartbeat_loop, name="vidfunnel-heartbeat", daemon=True)
            thread.start()
        except RuntimeError as error:  # no thread can be started (a pids limit, RLIMIT_NPROC)
            _beats.remove(beat)
            raise LockHeld("cannot start the heartbeat thread its lock needs ({}); the folder is not locked and "
                           "not worked on".format(error)) from error
        _beat_thread = thread


def _heartbeat_loop():
    """Beat every registered lock when it is due, and all of them after a suspend; end once none is left.

    The guard is held only to read and update the registry, never while the
    share is touched, so a stalled share cannot hold up release() or the
    start of a child (held_lock_fds). A descriptor in use by a beat is not
    closed by release(); the beat closes it afterwards. An exception from
    one lock's beat is reported once for that lock and the loop goes on
    with the others; whenever the loop ends, the thread slot is cleared,
    so the next lock taken starts a new thread.
    """
    global _beat_thread
    try:
        _beat_until_none_left()
    finally:
        with _held_locks_guard:
            if _beat_thread is threading.current_thread():
                _beat_thread = None


def _beat_until_none_left():
    global _beat_thread
    last_wall = time.time()
    last_mono = time.monotonic()
    while True:
        wall = time.time()
        now = time.monotonic()
        # Woken from a suspend: the wall clock counted the sleep and the monotonic clock did not (Linux and
        # macOS), so every lock is checked and beaten now rather than at its next interval, when another host
        # may have taken it over long ago. A wall clock stepped forward for another reason (NTP, a manual
        # change) only causes one early beat, which is harmless; one stepped backwards changes nothing. A
        # thread that was not scheduled (SIGSTOP, a debugger) needs no such check: the monotonic clock counts
        # that time, so a stop long enough for a takeover (HEARTBEAT_STALE_SECONDS) makes every lock due anyway.
        woke = (wall - last_wall) - (now - last_mono) > HEARTBEAT_WAKE_GAP_SECONDS
        last_wall, last_mono = wall, now
        with _held_locks_guard:
            if not _beats:
                _beat_thread = None
                return
            due = [beat for beat in _beats if woke or beat.due <= now]
            for beat in due:
                beat.beating = True
                beat.due = now + HEARTBEAT_SECONDS
            pause = min(beat.due for beat in _beats) - now
        for beat in due:
            try:
                _beat_once(beat)
            except Exception as error:
                try:
                    _beat_crashed(beat, error)
                except Exception:  # the report itself failed: the beats go on regardless
                    pass
            finally:
                with _held_locks_guard:
                    beat.beating = False
                    if beat.handover:
                        try:
                            os.close(beat.handle)
                        except OSError:
                            pass
        if not due:
            time.sleep(max(0.0, min(pause, HEARTBEAT_POLL_SECONDS)))


def _beat_once(beat):
    """Check that the lock path still names our file, then rewrite its first byte in place and fsync.

    The same byte at the same offset: the content stays valid, complete
    JSON for any reader at any instant, the inode stays (the flock, the
    children's descriptors and the takeover's identity check depend on
    it), and the write makes the file server stamp the modification time
    with its own clock (os.utime with an explicit time would stamp ours).
    A failed check or write is reported once per lock and tried again at
    the next interval; nothing here raises.
    """
    if beat.released or beat.lost:
        return
    state = _lock_state(beat)
    if state is not None:
        if state[0] == "lost":
            _lose_lock(beat, state[1])
        else:
            _beat_failed(beat, state[1])
        return
    try:
        os.pwrite(beat.handle, beat.first_byte, 0)
        os.fsync(beat.handle)
    except OSError as error:
        _beat_failed(beat, "cannot write the heartbeat: {}".format(error))
        return
    beat.count += 1


def _lock_state(beat):
    """None when the lock path names the file we hold; ('lost', words) or ('failed', words) otherwise.

    Lost: the path is missing while '.vidfunnel/' is there, it names
    another inode, or our descriptor's file is stale (NFS: it was removed on
    the server). An error that may be the share's (the directory is not
    there either, EIO) is a failure, retried at the next interval.
    """
    try:
        there = os.stat(beat.path)
    except FileNotFoundError:
        if not os.path.isdir(os.path.dirname(beat.path)):
            return "failed", "cannot reach '{}'".format(os.path.dirname(beat.path))
        try:
            there = os.stat(beat.path)  # the folder is there: it may have come back since the first look
        except FileNotFoundError:
            return "lost", "its lock file is gone"
        except OSError as error:
            return "failed", "cannot check the lock file: {}".format(error)
    except OSError as error:
        return "failed", "cannot check the lock file: {}".format(error)
    try:
        ours = os.fstat(beat.handle)
    except OSError as error:
        if error.errno == errno.ESTALE:
            return "lost", "its lock file was removed on the server"
        return "failed", "cannot check the lock file: {}".format(error)
    if (there.st_dev, there.st_ino) != beat.identity or (ours.st_dev, ours.st_ino) != beat.identity:
        return "lost", "its lock file is another run's now"
    return None


def _beat_failed(beat, words):
    if beat.failure_reported:
        return
    beat.failure_reported = True
    _warn("warning: lock '{}': {}; trying again every {} s".format(beat.path, words, HEARTBEAT_SECONDS))


def _beat_crashed(beat, error):
    if beat.crash_reported:
        return
    beat.crash_reported = True
    _warn("warning: lock '{}': the heartbeat failed ({}: {}); trying again every {} s".format(
        beat.path, type(error).__name__, error, HEARTBEAT_SECONDS))


def _lose_lock(beat, words):
    """Stop beating a lock another run has taken, say so once and drain the run (nothing further starts).

    Nothing is done when release() has begun meanwhile: it unlinks our own
    file before it closes the descriptor, and a beat that saw the path
    missing in that moment has not lost the lock.
    """
    with _held_locks_guard:
        if beat.released or beat.lost:  # released: see above; lost: another thread has already said so
            return
        beat.lost = True
        if beat in _beats:
            _beats.remove(beat)
    drain_for_lost_lock(str(beat.folder))  # first: nothing after it may keep the run from draining
    host = None
    try:
        holder = _read_json(beat.path)
        if isinstance(holder, dict) and isinstance(holder.get("host"), str):
            host = holder["host"]
    except Exception:  # unreadable, or nested too deep for json (RecursionError): the message has no host
        pass
    holder_words = "; host '{}' holds it now".format(host) if host is not None else ""
    _warn("vidfunnel: lost the lock of folder '{}': {}{}; finishing the items in progress, starting no "
          "more".format(beat.folder, words, holder_words))


def _folder_key(folder):
    return os.path.abspath(os.fspath(folder))


def lock_problem(folder):
    """None when this process may write in `folder`, else why not; checked now, on the calling thread.

    None also when this process holds no lock of `folder` (nothing to lose).
    Otherwise the same identity check as the heartbeat's (_lock_state: one
    stat of the lock path, one fstat of the held descriptor) runs at once.
    A lock found taken is handled as the heartbeat thread would (marked
    lost, the run drained, one line on stderr); a lock already marked lost
    answers without a look. A check that cannot decide (the share cannot be
    reached) is not safe either: its reason says the lock could not be
    checked.
    """
    with _held_locks_guard:
        beat = _folder_beats.get(_folder_key(folder))
        if beat is None:
            return None
        lost = beat.lost
    if not lost:
        state = _lock_state(beat)
        if state is None:
            return None
        if state[0] != "lost":
            return ("the lock of folder '{}' could not be checked ({}); nothing more of this item is written "
                    "there".format(beat.folder, state[1]))
        _lose_lock(beat, state[1])
    return "the lock of folder '{}' was taken over by another run; nothing more of this item is written there".format(
        beat.folder)


def ensure_lock(folder):
    """Raise LockLost when lock_problem(folder) finds a reason not to write in `folder`."""
    problem = lock_problem(folder)
    if problem is not None:
        raise LockLost(problem)


def _share_clock_ns(directory):
    """The share's current time in ns: st_mtime_ns of a new, empty probe file in `directory`, removed again.

    The file server stamps both this file and the lock file, so the two
    times compare on one clock whatever this machine's clock says. The name
    'lock.probe.<uuid4 hex>' cannot be a lock, a record ('.json') or
    another run's probe. OSError when the probe cannot be created or read.
    """
    name = os.path.join(str(directory), PROBE_PREFIX + uuid.uuid4().hex)
    handle = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        take_folder_mode(handle, Path(directory).parent)  # changes the inode's ctime, not its mtime
        return os.fstat(handle).st_mtime_ns
    finally:
        try:
            os.close(handle)
        finally:
            try:
                os.unlink(name)
            except OSError:
                pass


def _age_on_share_clock(directory, mtime_ns):
    """(seconds since mtime_ns, which clock) on the share's clock (a probe file), else on this machine's clock."""
    try:
        return (_share_clock_ns(directory) - mtime_ns) / 1e9, "the share's clock"
    except OSError:
        return (time.time_ns() - mtime_ns) / 1e9, "this machine's clock (no probe file could be created)"


def _takeover_step(step):
    """Called between the steps of a takeover (FolderLock._replace) with the step's name; does nothing.

    Tests patch it to widen the window between two steps or to act in it.
    """


class _Takeover:
    """What _create needs to put a taker's lock in place: its marker and the dead lock it judged."""

    def __init__(self, marker, judged, holder, age):
        self.marker = marker  # (descriptor, (st_dev, st_ino)) of our marker; the descriptor keeps the inode ours
        self.judged = judged  # the dead lock's identity (_identity_of)
        self.holder = holder  # its content
        self.age = age


def _marker_content(path, status):
    """The content dict of the takeover marker at `path`, whose lstat is `status`; None when it is gone.

    Only a regular file is read: a symlink is not followed and a FIFO not
    opened. Anything else, and a file whose JSON is unreadable (a taker that
    died before it wrote its marker, or one writing it now), nested too
    deeply, null or not an object, has the content {}: an empty marker. None
    when the file is gone by the time it is read.
    """
    if not stat.S_ISREG(status.st_mode):
        return {}
    try:
        info = _read_json(path)
    except RecordError:
        return {}
    if info is None and not os.path.lexists(path):
        return None
    return info if isinstance(info, dict) else {}


def _marker_writer(info):
    """Who wrote a takeover marker, for a message: "host 'h' pid p", or words for a marker that names nobody."""
    if info.get("host") is None and info.get("pid") is None:
        return "another run whose marker is empty or unreadable"
    return "host '{}' pid {}".format(info.get("host"), info.get("pid"))


def _claims_heartbeat(holder):
    """True when the lock says its holder beats (written by a vidfunnel with the heartbeat)."""
    return isinstance(holder.get(HEARTBEAT_FIELD), dict)


FLOCK_RETRY_SECONDS = 1.0
# After the rename of a takeover, a stat of the lock path that fails (EIO, ESTALE) is tried this often in all, this
# far apart, before the taker gives the lock up (FolderLock._path_names).
PATH_CHECK_ATTEMPTS = 3
PATH_CHECK_PAUSE_SECONDS = 0.1


def _take_flock(handle):
    """True once flock(LOCK_EX) is held on the new lock file's descriptor; False when it cannot be had.

    False without fcntl, or when the file system refuses flock (any OSError
    but a busy lock). A busy lock on a file this run has just created is a
    judge's momentary LOCK_SH test (see FolderLock._flock_judgement); it is
    retried for FLOCK_RETRY_SECONDS, then given up as refused.
    """
    if fcntl is None:
        return False
    deadline = time.monotonic() + FLOCK_RETRY_SECONDS
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        except OSError:
            return False


def pid_alive(pid):
    """True when a process with this pid exists here, False when none does, None when that cannot be told.

    os.kill(pid, 0) sends no signal; PermissionError means the process
    exists under another user. A pid that is not a positive int gives None,
    and so does one os.kill cannot take (OverflowError for a pid of 2**31 or
    more, from a crafted or corrupt lock file).
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError, ValueError):
        return None
    return True


def _identity_of(status):
    """(device, inode, mtime in ns, size) of an os.stat_result: the identity a takeover re-checks."""
    return (status.st_dev, status.st_ino, status.st_mtime_ns, status.st_size)


def _whole_seconds(age):
    return "{:.0f}".format(age) if age is not None else "an unknown number of"


def _unjudged_reason(host, holder_in_container, ours, pid):
    """Why a lock's pid could not be checked, for the LockHeld message."""
    if holder_in_container:
        return "held by a container run, whose pid cannot be checked from here; taken over once 24 h old"
    if host != ours["host"]:
        return "written on another host name, whose pid cannot be checked from here; taken over once 24 h old"
    if ours["container"]:
        return "written outside this container, whose pid cannot be checked from here; taken over once 24 h old"
    if pid == ours["pid"]:
        return "its pid is this process's own, so it cannot be checked; taken over once 24 h old"
    return "its pid cannot be checked; taken over once 24 h old"


def _written_before_reboot(holder, content):
    """True when the lock was written on this machine during an earlier boot, so its holder cannot run.

    The lock's machine must equal ours and come from /etc/machine-id, which
    stays the same across reboots: its machine_source says 'machine-id',
    or, in a lock without machine_source, its machine differs from its boot
    (machine_id() falls back to the boot id itself, and then a differing boot
    id says nothing about the machine). Its boot id must be present and
    differ from ours: a boot id is new on every boot.
    """
    machine = holder.get("machine")
    boot = holder.get("boot")
    if content["machine"] is None or machine != content["machine"]:
        return False
    if content["boot"] is None or not isinstance(boot, str) or not boot or boot == content["boot"]:
        return False
    if "machine_source" in holder:
        return holder["machine_source"] == "machine-id"
    return machine != boot


class FolderLock:
    """Context manager holding '.vidfunnel/lock' for one folder.

    The lock file holds host (socket.gethostname()), machine (machine_id()),
    machine_source ('machine-id', 'boot-id' or 'hardware', see
    machine_identity), pid, started (ISO time), container (True when this run is inside a
    container: '/run/.containerenv' or '/.dockerenv' exists), boot (boot_id()),
    device (the lock file's st_dev as the holder sees it) and flock (True
    when the holder holds flock(LOCK_EX) on the lock file).

    The holder keeps the descriptor it created the file with open for the
    whole run and takes flock(LOCK_EX) on it before it writes the contents,
    so a file whose contents say flock: true is flocked. The kernel releases
    that flock when the last descriptor of the open file is closed: when the
    holder and every child it started (they inherit the descriptor, see
    held_lock_fds) have exited, however they ended. A judge whose boot id
    equals the lock's (the same kernel, inside a container or not) therefore
    tests the flock itself, with flock(LOCK_SH | LOCK_NB) on a descriptor of
    its own: a busy flock raises LockHeld whatever the lock's age, since the
    kernel, not a pid, says a process holds it; a free one means every
    holder has exited, and the lock is taken over at once with a message
    naming the holder. The judge also requires the lock's device to be the
    one it sees the file on, so both runs reach the file through the same
    mounted file system (a bind mount into a container shares it). A flock
    does not cross kernels (another machine, a Mac's podman VM), so a lock
    with another boot id, without one, on another device, or with flock:
    false (fcntl missing, or a file system that refuses flock) is judged by
    the rules below.

    A lock written on this machine (its machine, from /etc/machine-id, is
    ours) under another boot id was written before this machine's last
    reboot: no process of that boot runs any more, so it is taken over at
    once with a message saying so, before the pid rule (a pid reused since
    the reboot must not count as alive) and before the age rule. A machine
    that came from the boot id fallback (machine_source 'boot-id', or, in a
    lock without machine_source, machine equal to boot) says nothing about
    the machine, so such a lock is left to the rules below. A container run
    sees the host's /etc/machine-id only when it is mounted in (the
    vidfunnel-podman wrapper does).

    Every other lock is taken over, with a warning, once it is 24 h old
    (stale_seconds); younger ones raise LockHeld. The pid can only shorten
    that wait, never lengthen it: when the lock was written on this host
    name and on the same side of the container boundary, a holder that no
    longer runs (os.kill(pid, 0) raises ProcessLookupError) is taken over at
    once with a message naming the dead pid and the lock's age. A live pid
    (or one we may not signal, PermissionError) is respected until the lock
    is stale, like any other; after 24 h it is taken for a reused pid. Our
    own pid, or one that cannot be checked, counts as unknown, and the lock
    is judged by age alone. A lock from another host name is judged by age
    alone too, since its pid means nothing here: a container run shows the
    container id as its host name and has a pid namespace of its own; so is
    a lock with the same host name written by a run inside a container, and
    a lock written on the host when this run is inside a container.

    Heartbeat. A lock written by this version carries "heartbeat":
    {"interval": HEARTBEAT_SECONDS}, and while it is held one daemon thread
    per process (started with the first lock, ended after the last release)
    rewrites the first byte of each held lock file in place through the
    held descriptor and fsyncs it every HEARTBEAT_SECONDS, and at the first
    poll after a suspend (the wall clock moved HEARTBEAT_WAKE_GAP_SECONDS more
    than the monotonic clock), so the file server advances the file's
    mtime. Before each beat it checks that the
    lock path still names the file it holds; when it does not, another run
    has taken the folder over: the thread stops beating that lock, says so
    on stderr and drains the run (vidfunnel.interrupt.drain_for_lost_lock),
    and release() leaves the file at the path alone. A heartbeat lock that
    the flock, reboot and pid rules above leave undecided is judged by its
    last beat instead of the 24 h rule: the judge creates a probe file
    beside it and compares the probe's mtime with the lock's, both stamped
    by the file server, and takes the lock for dead once that is more than
    HEARTBEAT_STALE_SECONDS; younger, it raises LockHeld whatever the lock's
    age. A pid running here keeps a lock with a silent beat until the 24 h
    rule. Where no probe file can be created, the 24 h rule applies to the
    younger of the lock's start and its last beat on this machine's clock.
    A lock without the field (an older vidfunnel) is judged as before.

    Where no lock file exists, a run creates it at the lock path with
    O_CREAT | O_EXCL, so of several runs that find no lock, one creates it
    and the others raise LockHeld naming it. A lock judged dead is replaced
    (see _replace): under flock() on the regular file '.vidfunnel/lock.takeover'
    (else on the directory), which serialises takeovers on one kernel, the
    run creates the marker '.vidfunnel/lock.taking' with O_EXCL, which
    serialises them across machines; checks again that the lock is the one
    it judged (inode, mtime, size and content); writes its own lock to a
    temporary file, flocks and fsyncs it, and renames it over the lock path,
    so the path names the old lock and then the new one and is never empty;
    and removes its marker. A run that gets the flock or finds the marker
    after it judges the new, live lock, or the marker, and raises LockHeld.
    The lock file is removed on exit, including
    on exception, if it is still ours: unlinked first and its descriptor
    closed after, so the file never exists with its flock free while this
    run holds it. The holder never opens and closes its own lock file while
    it holds it (a judge in this process that finds a lock file this process
    holds raises LockHeld without opening it, and release() reads the file
    through the kept descriptor), since on NFS, where Linux emulates flock
    with POSIX locks, closing any descriptor of a file can drop them.
    """

    def __init__(self, folder, warn=None, stale_seconds=LOCK_STALE_SECONDS):
        self.folder = Path(folder)
        self.path = record_dir(folder) / LOCK_NAME
        self.warn = warn if warn is not None else _warn
        self.stale_seconds = stale_seconds
        self.content = None
        self.handle = None  # the descriptor of our lock file while we hold it
        self.beat = None    # its entry in the heartbeat registry while we hold it
        self.took_over = False  # True once acquire() replaced a lock judged dead (vidfunnel.turns reports it)

    def _holder(self):
        """(content dict, age in seconds) of the current lock file, or (None, None) if absent."""
        try:
            data = _read_json(self.path)
        except RecordError:
            data = {}
        if data is None:
            if not self.path.exists():
                return None, None
            data = {}  # a lock file holding JSON null is judged by its age like any other unreadable one
        if not isinstance(data, dict):
            data = {}
        started = data.get("started")
        age = None
        if isinstance(started, str):
            try:
                age = (datetime.datetime.now(datetime.timezone.utc) - parse_iso(started)).total_seconds()
            except ValueError:
                age = None
        if age is None:
            try:
                age = datetime.datetime.now().timestamp() - self.path.stat().st_mtime
            except FileNotFoundError:
                return None, None
        return data, age

    def _identity(self):
        """(device, inode, mtime in ns, size) of the lock file, or None when it does not exist."""
        try:
            status = os.stat(str(self.path))
        except FileNotFoundError:
            return None
        return _identity_of(status)

    @contextlib.contextmanager
    def _takeover_guard(self):
        """Hold flock(LOCK_EX) while one takeover runs; see the class docstring.

        The flock is taken on the regular file '.vidfunnel/lock.takeover'
        (created if absent; it stays, empty), which more file systems support
        than a flock on a directory, NFS among them; the directory is the
        second choice. When neither works the takeover runs without it,
        guarded by the marker and the identity re-checks (_replace).
        Yields which flock is held: 'file', 'directory', or None for none.
        """
        handle = None
        held = None
        if fcntl is not None:
            targets = ((str(record_dir(self.folder) / TAKEOVER_FILE), os.O_RDWR | os.O_CREAT, "file"),
                       (str(record_dir(self.folder)), os.O_RDONLY, "directory"))
            for target, flags, kind in targets:
                try:
                    handle = os.open(target, flags, 0o644)
                    if kind == "file":
                        take_folder_mode(handle, self.folder)
                    fcntl.flock(handle, fcntl.LOCK_EX)
                    held = kind
                    break
                except OSError:
                    if handle is not None:
                        os.close(handle)
                    handle = None
        try:
            yield held
        finally:
            if handle is not None:
                os.close(handle)  # closing the descriptor releases the flock

    def _create(self, content, takeover=None):
        """Create our lock file, flock it, write our entry; FileExistsError when any lock file exists.

        Sets content["flock"] to whether the flock was taken and
        content["device"] to the file's st_dev, and keeps the descriptor open in self.handle and in the registry of held_lock_fds()
        until release(). A file whose entry could not be written is unlinked
        before its descriptor is closed.

        With `takeover` (a _Takeover, from _replace) the file is created under
        a unique temporary name ('lock.new.<uuid4 hex>') instead, written in
        full and fsynced, and renamed over the lock path with os.replace once
        the marker is still ours and the lock still the one judged; after the
        rename the lock path must name our file, else LockHeld naming the run
        whose file it names. A failure before the rename unlinks the temporary
        file and leaves the lock path alone; one after it (or one raised as
        os.replace returns, which finds the temporary name gone) unlinks our
        file from the lock path only while the path still names it. A lock
        path whose stat keeps failing (_path_names) is not ours to keep or to
        remove: the OSError is raised and the path left as it is.
        """
        if takeover is None:
            name = str(self.path)
        else:
            name = str(record_dir(self.folder) / (NEW_LOCK_PREFIX + uuid.uuid4().hex))
        handle = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644)
        beat = None
        renamed = False
        status = None
        try:
            take_folder_mode(handle, self.folder)
            if takeover is not None:
                _takeover_step("created")
            content["flock"] = _take_flock(handle)
            status = os.fstat(handle)
            content["device"] = status.st_dev
            data = _dump_json(content).encode("utf-8")
            beat = _Beat(handle, self.path, self.folder, status, data[:1])
            while data:
                data = data[os.write(handle, data):]
            if takeover is not None:
                os.fsync(handle)
                _takeover_step("written")
                self._check_marker(takeover.marker)
                self._recheck(takeover.judged, takeover.holder, takeover.age)
                _takeover_step("checked")
                try:
                    os.replace(name, str(self.path))
                except FileNotFoundError:
                    raise LockHeld("folder '{}': this run's new lock file '{}' was removed before it could be put "
                                   "in place (another run took it for a leftover); not taken over".format(
                                       self.folder, name))
                renamed = True
                _takeover_step("renamed")
                if not self._path_names(status):
                    raise self._lost_race()
            with _held_locks_guard:
                _held_locks[handle] = (status.st_dev, status.st_ino)
                _start_beating(beat)
                _folder_beats[_folder_key(self.folder)] = beat
        except BaseException:
            with _held_locks_guard:
                _held_locks.pop(handle, None)
                if beat in _beats:
                    _beats.remove(beat)
                if beat is not None and _folder_beats.get(_folder_key(self.folder)) is beat:
                    del _folder_beats[_folder_key(self.folder)]
            try:
                if takeover is None:
                    os.unlink(str(self.path))
                elif not renamed:
                    try:
                        os.unlink(name)
                    except FileNotFoundError:
                        # Renamed after all: the exception came as os.replace returned (a KeyboardInterrupt). Our
                        # file goes from the lock path only while the path names it.
                        if status is not None and self._path_names(status):
                            os.unlink(str(self.path))
                elif status is not None and self._path_names(status):
                    os.unlink(str(self.path))
            except OSError:  # a path that cannot be shown to name our file is left as it is
                pass
            os.close(handle)
            raise
        self.handle = handle
        self.beat = beat
        self.content = content

    def _held(self, holder, age, why):
        host = holder.get("host")
        holder_words = "a container run on host" if holder.get("container") is True else "host"
        return LockHeld(
            "folder '{}' is locked by {} '{}' pid {} since {} ({:.0f} s ago; {}); lock file '{}'".format(
                self.folder, holder_words, host, holder.get("pid"), holder.get("started"), age, why, self.path),
            host=host, pid=holder.get("pid"), age_seconds=age)

    def _lost_race(self):
        """LockHeld for the run that created the lock file while we were taking it over."""
        holder, age = self._holder()
        if holder is None:
            return LockHeld("folder '{}': its lock file '{}' was removed by another run while this run was taking "
                            "it over; not taken over".format(self.folder, self.path))
        return self._held(holder, age, "it took the lock over at the same time and got it first")

    def acquire(self):
        make_record_dir(self.folder)
        machine, machine_source = machine_identity()
        content = {"host": socket.gethostname(), "pid": os.getpid(), "started": now_iso(),
                   "container": in_container(), "machine": machine, "machine_source": machine_source,
                   "boot": boot_id(), HEARTBEAT_FIELD: {"interval": HEARTBEAT_SECONDS}}
        self.took_over = False
        try:
            self._create(content)
        except FileExistsError:
            with self._takeover_guard() as guard:
                self._take_over(content, guard)
        self.content = content
        return self

    def _warn_quietly(self, line):
        """self.warn(line), dropping an OSError from it (an output stream that cannot be written).

        Used where the lock may already be ours: a warning that cannot be
        written must not make acquire() raise while the lock is held.
        """
        try:
            self.warn(line)
        except OSError:
            pass

    def _take_over(self, content, guard=None):
        """Judge the existing lock; take it over (_replace) or raise LockHeld.

        `guard`: which takeover flock this run holds (_takeover_guard).
        """
        while True:
            judged = self._identity()
            if judged is not None and _held_here(judged[0], judged[1]):
                raise LockHeld("folder '{}' is locked by this run itself, which already holds lock file '{}'".format(
                    self.folder, self.path), host=content["host"], pid=content["pid"], age_seconds=None)
            holder, age = self._holder()
            if holder is None or judged is None:  # released meanwhile: create it, or judge the next one
                try:
                    self._create(content)
                    return
                except FileExistsError:
                    continue
            host = holder.get("host")
            pid = holder.get("pid")
            holder_in_container = holder.get("container") is True
            holder_words = "a container run on host" if holder_in_container else "host"
            judgement = self._flock_judgement(holder, content, judged)
            if judgement == "replaced":
                continue
            if judgement == "held":
                raise self._held(holder, age, "its holder, or a process it started, still holds the lock file's "
                                 "flock on this machine; not taken over while it does, whatever its age")
            liveness = None
            rebooted = judgement is None and _written_before_reboot(holder, content)
            same_machine = content["machine"] is not None and holder.get("machine") == content["machine"]
            if (judgement is None and not rebooted and host == content["host"] and same_machine
                    and holder_in_container == content["container"] and pid != content["pid"]):
                liveness = pid_alive(pid)
            beat_age = None    # seconds since the holder's last beat, on the share's clock
            clock_doubt = None  # why the share's clock cannot judge this heartbeat lock
            rule_age = age     # what the 24 h rule compares
            if judgement is None and not rebooted and liveness is not False and _claims_heartbeat(holder):
                try:
                    beat_age = (_share_clock_ns(record_dir(self.folder)) - judged[2]) / 1e9
                except OSError as error:
                    clock_doubt = "no probe file can be created in '{}' to read the share's clock ({})".format(
                        record_dir(self.folder), error)
                else:
                    if beat_age < -HEARTBEAT_AHEAD_SECONDS:  # stamped in the share's future: its clock stepped back
                        clock_doubt = "its last beat is {:.0f} s ahead of the share's clock".format(-beat_age)
                        beat_age = None
                if clock_doubt is not None:
                    # The 24 h rule on the younger of start and last beat on our clock (a beating holder is never
                    # older than its beat); a last beat ahead of our clock too cannot shorten the age.
                    beat_here = time.time() - judged[2] / 1e9
                    if beat_here >= -HEARTBEAT_AHEAD_SECONDS:
                        rule_age = min(age, beat_here)
            if judgement == "free":
                warning = ("warning: taking over lock '{}': its holder, {} '{}' pid {}, has exited (nothing "
                           "holds the lock file's flock; lock written {} s ago)".format(
                               self.path, holder_words, host, pid, _whole_seconds(age)))
            elif rebooted:
                warning = ("warning: taking over lock '{}': its holder, {} '{}' pid {}, ran before this "
                           "machine's last reboot (lock written {} s ago)".format(
                               self.path, holder_words, host, pid, _whole_seconds(age)))
            elif liveness is False:
                warning = ("warning: taking over lock '{}': its holder, host '{}' pid {}, no longer runs "
                           "(lock written {} s ago)".format(self.path, host, pid, _whole_seconds(age)))
            elif beat_age is not None and beat_age <= HEARTBEAT_STALE_SECONDS:
                held = self._held(holder, age, "{}its holder last beat {:.0f} s ago on the share's clock; taken over "
                                  "once it has been silent for {:.0f} s".format(
                                      "its pid {} is running here and ".format(pid) if liveness is True else "",
                                      max(0.0, beat_age), HEARTBEAT_STALE_SECONDS))
                if liveness is not True:  # with its pid running here the 24 h rule takes it over, not the beat
                    held.beat_age_seconds = max(0.0, beat_age)
                raise held
            elif beat_age is not None and liveness is True and age < self.stale_seconds:
                raise self._held(holder, age, "its pid {} is running here, although its holder last beat {:.0f} s "
                                 "ago on the share's clock; taken over once 24 h old".format(pid, beat_age))
            elif beat_age is not None:
                warning = ("warning: taking over lock '{}' held by {} '{}' pid {} since {}: its holder last beat "
                           "{:.0f} s ago on the share's clock, more than {:.0f} s{}".format(
                               self.path, holder_words, host, pid, holder.get("started"), beat_age,
                               HEARTBEAT_STALE_SECONDS,
                               "; its pid is running here, taken for a reused one" if liveness is True else ""))
            elif rule_age < self.stale_seconds:
                if liveness is True:
                    why = "its pid {} is running here; taken over once 24 h old".format(pid)
                elif host == content["host"] and not same_machine:
                    why = ("written on another machine with this host name (or before the machine id was "
                           "recorded), whose pid cannot be checked from here; taken over once 24 h old")
                else:
                    why = _unjudged_reason(host, holder_in_container, content, pid)
                if clock_doubt is not None:
                    why += "; its heartbeat cannot be judged, since {}, so the 24 h rule applies to its last beat".format(
                        clock_doubt)
                raise self._held(holder, age, why)
            elif clock_doubt is not None:
                warning = ("warning: taking over stale lock '{}' held by {} '{}' pid {} since {}: {}, so the 24 h "
                           "rule decided on the younger of its start and its last beat, {:.1f} h ago on this "
                           "machine's clock{}".format(
                               self.path, holder_words, host, pid, holder.get("started"), clock_doubt,
                               rule_age / 3600.0,
                               "; its pid is running here, taken for a reused one" if liveness is True else ""))
            else:
                warning = "warning: taking over stale lock '{}' held by {} '{}' pid {} since {} ({:.1f} h ago{})".format(
                    self.path, holder_words, host, pid, holder.get("started"), age / 3600.0,
                    "; its pid is running here, taken for a reused one" if liveness is True else "")
            self._replace(content, judged, holder, age, guard)
            self.took_over = True
            self._warn_quietly(warning)  # the lock is ours: a warning that cannot be written does not undo that
            return

    def _flock_judgement(self, holder, content, judged):
        """'held' or 'free' from the lock file's flock, 'replaced', or None when it cannot be tested here.

        Tested only when the lock says flock: true, carries our boot id (the
        same kernel) and names the device the file is on here (the same
        mounted file system: a share mounted twice, each mount keeping its
        locks to itself as sshfs does, shows two devices).
        'held': another open file holds the flock, so its holder or a child
        of it is alive. 'free': none does, so every one of them has exited
        (the flock is taken before the contents that say flock: true are
        written). 'replaced': the file at the path is no longer the one
        judged; the caller judges again. The test descriptor is closed in
        every case, which also releases the shared flock a 'free' test took.
        """
        if fcntl is None or holder.get("flock") is not True:
            return None
        if content["boot"] is None or holder.get("boot") != content["boot"] or holder.get("device") != judged[0]:
            return None
        try:
            test = os.open(str(self.path), os.O_RDONLY)
        except FileNotFoundError:
            return "replaced"
        except OSError:
            return None
        try:
            if _identity_of(os.fstat(test)) != judged:
                return "replaced"
            try:
                fcntl.flock(test, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return "held"
            except OSError:
                return None
            return "free"
        finally:
            os.close(test)

    def _recheck(self, judged, holder, age):
        """LockHeld unless the lock file is still the one judged dead (inode, mtime, size and content).

        A file with the same inode and the same content whose mtime moved,
        when the lock says its holder beats, means the holder beat meanwhile
        and is alive. Other content under the same inode number is another
        run's new file: a file system may hand a freed inode number to the
        next file created (ext4 does), so an older vidfunnel's unlink and
        create can leave one.
        """
        current = self._identity()
        holder_now = self._holder()[0]
        if current != judged or holder_now != holder:
            if (current is not None and current[:2] == judged[:2] and holder_now == holder
                    and _claims_heartbeat(holder)):
                raise self._held(holder, age, "its holder beat while the lock was being judged; not taken over")
            raise self._lost_race()

    def _path_names(self, status):
        """True when the lock path names the file with this os.stat_result's st_dev and st_ino.

        `status` is the fstat of our own descriptor. A stat of the path that
        fails for another reason than a missing file (EIO, ESTALE) is tried
        PATH_CHECK_ATTEMPTS times in all, PATH_CHECK_PAUSE_SECONDS apart; then
        its OSError is raised: the path cannot be shown to name our file.
        """
        for attempt in range(PATH_CHECK_ATTEMPTS):
            try:
                there = os.stat(str(self.path))
            except FileNotFoundError:
                return False
            except OSError:
                if attempt + 1 >= PATH_CHECK_ATTEMPTS:
                    raise
                time.sleep(PATH_CHECK_PAUSE_SECONDS)
                continue
            return (there.st_dev, there.st_ino) == (status.st_dev, status.st_ino)

    def _replace(self, content, judged, holder, age, guard=None):
        """Take over the lock judged dead, or raise LockHeld (or OSError) and leave the lock path alone.

        A: create the marker '.vidfunnel/lock.taking' with O_EXCL (_take_marker);
        only its creator may replace the lock. B: check again that the lock is
        the one judged (_recheck). C: write our lock to a temporary file,
        flock and fsync it. D: check that the marker is still ours and the
        lock still the one judged, rename our file over the lock path, and
        check that the path names it (C and D in _create). E: remove our
        marker if it is still ours (_drop_marker), on every path out. Leftovers
        of runs that died mid-takeover are removed under the marker. `guard`:
        which takeover flock this run holds (_takeover_guard).
        """
        marker = self._take_marker(content, guard)
        try:
            _takeover_step("marked")
            self._recheck(judged, holder, age)
            _takeover_step("rechecked")
            self._clear_leftovers()
            self._create(content, _Takeover(marker, judged, holder, age))
            _takeover_step("published")
        finally:
            self._drop_marker(marker)

    def _marker_path(self):
        return str(record_dir(self.folder) / TAKEOVER_MARKER)

    def _take_marker(self, content, guard=None):
        """Create the takeover marker with O_EXCL; (descriptor, (st_dev, st_ino)) once it is ours and written.

        The marker holds our host, pid, a random token, the time, our machine,
        machine_source and boot id (as in the lock), the marker's st_dev and
        which takeover flock we hold (`guard`). An existing marker is another
        run's takeover in progress: LockHeld naming its host, unless it is
        older than TAKEOVER_MARKER_STALE_SECONDS on the share's clock (else on
        ours), or its takeover has ended on this kernel (_marker_ended_here);
        such a marker is moved aside (_move_marker_aside) and the exclusive
        create is tried again, once. The descriptor stays open until
        _drop_marker, so the inode cannot be reused meanwhile.
        """
        path = self._marker_path()
        cleared = False
        for _attempt in range(3):
            try:
                handle = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                found = self._judge_marker(path)
                if found is None:  # removed since: its takeover ended; compete for the marker
                    continue
                identity, info, age, clock = found
                stale = age > TAKEOVER_MARKER_STALE_SECONDS
                ended = not stale and self._marker_ended_here(identity, info, content, guard)
                if cleared or not (stale or ended):
                    raise self._marker_held(info, age, clock)
                self._move_marker_aside(path, identity, info, age, clock, ended)
                cleared = True
                continue
            identity = None
            try:
                take_folder_mode(handle, self.folder)
                status = os.fstat(handle)
                identity = (status.st_dev, status.st_ino)
                data = _dump_json({"host": content["host"], "pid": content["pid"], "token": uuid.uuid4().hex,
                                   "started": now_iso(), "machine": content["machine"],
                                   "machine_source": content["machine_source"], "boot": content["boot"],
                                   "device": status.st_dev, "guard": guard}).encode("utf-8")
                while data:
                    data = data[os.write(handle, data):]
                os.fsync(handle)
            except BaseException:
                try:
                    there = os.stat(path)
                    if identity is not None and (there.st_dev, there.st_ino) == identity:
                        os.unlink(path)
                except OSError:
                    pass
                os.close(handle)
                raise
            return handle, identity
        raise LockHeld("folder '{}': the takeover marker '{}' kept changing while this run tried to create it; "
                       "another takeover is in progress, not taken over".format(self.folder, path))

    def _judge_marker(self, path):
        """(identity, content dict, age in seconds, clock words) of an existing marker; None when it is gone.

        The path is looked at with lstat and never followed: a symlink, a
        FIFO or anything else that is not a regular file has the content {}
        (_marker_content) and its own mtime, and ages out like an empty marker.
        """
        try:
            status = os.lstat(path)
        except FileNotFoundError:
            return None
        info = _marker_content(path, status)
        if info is None:
            return None
        age, clock = _age_on_share_clock(record_dir(self.folder), status.st_mtime_ns)
        return _identity_of(status), info, age, clock

    @staticmethod
    def _marker_ended_here(identity, info, content, guard):
        """True when the marker's takeover ran on this kernel under the takeover flock we now hold, so it has ended.

        Every taker holds its takeover flock (_takeover_guard) from before it
        creates its marker until after it removes it, and the kernel frees
        that flock when the taker dies. While this run holds the same flock no
        takeover that took it is in progress: its run died or finished. The
        marker must name that same flock ('guard': the file, or the directory
        where the file could not be flocked), our boot id (the same kernel; a
        boot id is never shared by two machines or two boots), our machine (a
        machine from the boot id fallback says no more than the boot id
        itself, and a container without the host's /etc/machine-id records
        another machine, so it waits for the stale rule), and the device we
        see the marker on (one mounted file system: two mounts of one share
        may keep their flocks apart). A marker without these fields (empty,
        unreadable, or from vidfunnel 20261110) is left to the stale rule.
        """
        device = info.get("device")
        if guard is None or info.get("guard") != guard:
            return False
        if content["boot"] is None or info.get("boot") != content["boot"]:
            return False
        if content["machine"] is None or info.get("machine") != content["machine"]:
            return False
        return isinstance(device, int) and not isinstance(device, bool) and device == identity[0]

    def _marker_held(self, info, age, clock):
        host = info.get("host")
        pid = info.get("pid")
        return LockHeld(
            "folder '{}' is being taken over by {}: a takeover is in progress (its marker '{}' was written {:.0f} s "
            "ago on {}; a marker older than {} s is cleared)".format(
                self.folder, _marker_writer(info), self._marker_path(), age, clock, TAKEOVER_MARKER_STALE_SECONDS),
            host=host, pid=pid, age_seconds=age)

    def _move_marker_aside(self, path, identity, info, age, clock, ended_here=False):
        """Clear a stale marker, or one whose takeover has ended on this kernel (`ended_here`): rename it to a
        unique name, check it is the one judged, remove it.

        Of several runs that rename the same marker, one moves it; the others
        find nothing at the path (and compete for a fresh marker) or move
        whatever is there by then. A moved file that is not the stale marker
        judged (same identity and token) is another run's fresh marker: it is
        left where it now is, under the aside name, and LockHeld is raised.
        That run then finds its marker gone at its check before the rename
        and gives up as well; the aside file is a leftover a later takeover
        removes once it is older than TAKEOVER_MARKER_STALE_SECONDS.
        """
        aside = str(record_dir(self.folder) / (MARKER_ASIDE_PREFIX + uuid.uuid4().hex))
        try:
            os.rename(path, aside)
        except FileNotFoundError:
            return
        _takeover_step("moved-aside")
        try:
            status = os.lstat(aside)
        except FileNotFoundError:
            # Removed meanwhile by another run's sweep of leftovers, which takes only files older than the
            # threshold: what was moved was a stale marker, not a fresh one. Compete for the marker.
            return
        except OSError:
            status = None
        moved_info = {}
        if status is not None:
            moved_info = _marker_content(aside, status)
            if moved_info is None:  # removed by such a sweep between the lstat and the read: as above
                return
        same = status is not None and _identity_of(status) == identity
        if same and (info or moved_info):  # an empty or unreadable marker has no token: its identity decides
            same = moved_info.get("token") == info.get("token")
        if not same:
            raise LockHeld(
                "folder '{}': the takeover marker '{}' was replaced while this run cleared it as stale; another "
                "takeover is in progress, not taken over (the marker moved is left as '{}')".format(
                    self.folder, path, aside))
        try:
            os.unlink(aside)
        except FileNotFoundError:
            pass
        except OSError as error:  # not a file (a directory): the marker path is clear all the same
            self.warn("warning: cannot remove '{}', moved aside from the takeover marker's path: {}".format(
                aside, error))
        if ended_here:
            self.warn("warning: cleared the takeover marker '{}' of {}: written {:.0f} s ago on {} by a takeover on "
                      "this machine since its last boot, which has ended (nothing here holds the takeover "
                      "flock)".format(path, _marker_writer(info), age, clock))
        else:
            self.warn("warning: cleared the takeover marker '{}' of {}: written {:.0f} s ago on {}, more than {} s; "
                      "the run that wrote it stopped mid-takeover".format(
                          path, _marker_writer(info), age, clock, TAKEOVER_MARKER_STALE_SECONDS))

    def _check_marker(self, marker):
        """LockHeld unless the marker path still names our marker (the inode our descriptor keeps)."""
        path = self._marker_path()
        try:
            there = os.stat(path)
        except FileNotFoundError:
            there = None
        if there is None or (there.st_dev, there.st_ino) != marker[1]:
            raise LockHeld("folder '{}': this run's takeover marker '{}' is no longer at its path (another run "
                           "cleared it as stale); the lock is left as it is, not taken over".format(
                               self.folder, path))

    def _drop_marker(self, marker):
        """Close our marker's descriptor and remove the marker if the path still names it; never raises OSError."""
        path = self._marker_path()
        handle, identity = marker
        there = None
        try:
            there = os.stat(path)
        except FileNotFoundError:
            pass
        except OSError as error:
            self._warn_quietly("warning: cannot check the takeover marker '{}': {}; a marker older than {} s is "
                               "cleared by the next takeover".format(path, error, TAKEOVER_MARKER_STALE_SECONDS))
        finally:
            try:
                os.close(handle)
            except OSError:  # an error from close (EIO on NFS) still frees the descriptor
                pass
        if there is None or (there.st_dev, there.st_ino) != identity:
            return
        _takeover_step("unmarking")
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as error:
            self._warn_quietly("warning: cannot remove the takeover marker '{}': {}; a marker older than {} s is "
                               "cleared by the next takeover".format(path, error, TAKEOVER_MARKER_STALE_SECONDS))

    def _clear_leftovers(self):
        """Remove temporary lock files and moved-aside markers older than TAKEOVER_MARKER_STALE_SECONDS.

        Runs under our marker. Their age is read on the share's clock (else
        ours); a younger one may belong to a taker still at work and stays.
        """
        directory = record_dir(self.folder)
        try:
            names = sorted(name for name in os.listdir(str(directory)) if _is_takeover_leftover(name))
        except OSError:
            return
        if not names:
            return
        try:
            now = _share_clock_ns(directory)
        except OSError:
            now = time.time_ns()
        for name in names:
            path = str(directory / name)
            try:
                if (now - os.lstat(path).st_mtime_ns) / 1e9 > TAKEOVER_MARKER_STALE_SECONDS:
                    os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError as error:
                self.warn("warning: cannot remove '{}', left by a takeover that did not finish: {}".format(path, error))

    def _still_ours(self):
        """True when the file at the lock path is the one we created and still holds our entry.

        Read through our own descriptor, never by opening the path (see the
        class docstring); any error reading it counts as not ours.
        """
        try:
            ours = os.fstat(self.handle)
            there = os.stat(str(self.path))
            if (ours.st_dev, ours.st_ino) != (there.st_dev, there.st_ino):
                return False
            os.lseek(self.handle, 0, os.SEEK_SET)
            chunks = []
            while True:
                chunk = os.read(self.handle, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
            return json.loads(b"".join(chunks).decode("utf-8")) == self.content
        except (OSError, ValueError):
            return False

    def release(self):
        """Unlink the lock file if it is still ours, then close its descriptor (which frees our flock).

        The heartbeat stops first. A lock the heartbeat found taken by
        another run is left alone without a look at its path; any other is
        unlinked only when the path still names our file with our entry
        (_still_ours, which stats the path and reads the file through our
        descriptor, so a stalled share does hold it up). A descriptor a beat
        is using at that moment is closed by the heartbeat thread once the
        beat ends, so release does not wait for a beat in flight.
        """
        if self.content is None:
            return
        handle = self.handle
        beat = self.beat
        lost = False
        if beat is not None:
            with _held_locks_guard:
                beat.released = True
                if beat in _beats:
                    _beats.remove(beat)
                if _folder_beats.get(_folder_key(self.folder)) is beat:
                    del _folder_beats[_folder_key(self.folder)]
                lost = beat.lost
        try:
            if lost:
                self.warn("warning: lock '{}' was taken over by another run; left in place".format(self.path))
            elif handle is not None and self._still_ours():
                try:
                    os.unlink(str(self.path))
                except FileNotFoundError:
                    pass
            else:
                self.warn("warning: lock '{}' no longer holds this run's entry; left in place".format(self.path))
        finally:
            self.content = None
            self.handle = None
            self.beat = None
            if handle is not None:
                with _held_locks_guard:
                    _held_locks.pop(handle, None)
                    if beat is not None and beat.beating:
                        beat.handover = True
                    else:
                        os.close(handle)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
        return False

# End of File #
