"""Per-folder JSON records, fingerprints and the folder lock (HANDOFF.md section 6.7).

Each processed folder holds a hidden '.vidfunnel/' directory with one JSON record
per source file, named after the full source file name including its extension
('movie.avi.json'), plus 'folder.json' and, while a run is active, 'lock'.
Records are the authority; every write is atomic (temp file, then os.replace).
"""

VERSION = "20261108"

import contextlib
import datetime
import hashlib
import json
import os
import socket
import sys
import tempfile
import uuid
from pathlib import Path

try:
    import fcntl
except ImportError:  # not on Windows, which vidfunnel does not support; the takeover is then unserialised
    fcntl = None

import vidfunnel
from vidfunnel.interrupt import check as check_interrupted

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


class RecordError(Exception):
    """A record or folder file exists but cannot be read or parsed."""


class LockHeld(Exception):
    """Another run holds the folder lock and it is younger than 24 hours."""

    def __init__(self, message, host=None, pid=None, age_seconds=None):
        Exception.__init__(self, message)
        self.host = host
        self.pid = pid
        self.age_seconds = age_seconds


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


# ---------------------------------------------------------------- JSON I/O


def _dump_json(data):
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _write_json_atomic(path, data):
    """Write data as JSON to path through a temp file in the same directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _dump_json(data)
    handle, temp_name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
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
    """Parsed JSON of path, None when it does not exist, RecordError when unreadable."""
    path = Path(path)
    try:
        with open(str(path), "r", encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise RecordError("cannot read '{}': {}".format(path, error))


# ---------------------------------------------------------------- records


def load_record(source):
    """The record of a source, or None when it has none. RecordError when it is unreadable."""
    path = record_path(source)
    data = _read_json(path)
    if data is not None and not isinstance(data, dict):
        raise RecordError("record '{}' is not a JSON object".format(path))
    return data


def save_record(source, record):
    """Write the record of a source atomically, creating '.vidfunnel/' when needed."""
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


FINGERPRINT_KEYS = ("size", "mtime_ns", "head_sha256", "tail_sha256")


def fingerprint_matches(first, second):
    """True when both fingerprints are present and agree on every field."""
    if not first or not second:
        return False
    for key in FINGERPRINT_KEYS:
        if first.get(key) is None or first.get(key) != second.get(key):
            return False
    return True


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
    """Write '.vidfunnel/folder.json' atomically."""
    _write_json_atomic(record_dir(folder) / FOLDER_FILE, data)


# ---------------------------------------------------------------- lock


def _warn(message):
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


# Files a container runtime leaves in the container's root: podman's and Docker's.
CONTAINER_MARKERS = ("/run/.containerenv", "/.dockerenv")
TAKEOVER_FILE = "lock.takeover"  # the regular file the takeover flock is taken on, beside the lock


def in_container():
    """True when this process runs inside a podman or Docker container (one of CONTAINER_MARKERS exists)."""
    return any(os.path.exists(marker) for marker in CONTAINER_MARKERS)


MACHINE_ID_FILES = ("/etc/machine-id", "/proc/sys/kernel/random/boot_id")


def machine_id():
    """An identity of this machine for the lock file, or None when none can be found.

    /etc/machine-id, else the kernel boot id (Linux), else the hardware
    address uuid.getnode() reports (macOS has neither file), unless that is
    the random value getnode falls back to (multicast bit set). Two machines
    on one share may carry the same host name (fresh installs, images); a
    pid is judged alive or dead only when the lock was written on this very
    machine, which the host name alone cannot tell.
    """
    for path in MACHINE_ID_FILES:
        try:
            with open(path, encoding="ascii", errors="replace") as handle:
                value = handle.read().strip()
        except OSError:
            continue
        if value:
            return value
    node = uuid.getnode()
    if (node >> 40) & 1:  # a random 48-bit value, not a hardware address
        return None
    return "node-{:012x}".format(node)


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


class FolderLock:
    """Context manager holding '.vidfunnel/lock' for one folder.

    The lock file holds host (socket.gethostname()), machine (machine_id()), pid, started (ISO time)
    and container (True when this run is inside a container: '/run/.containerenv'
    or '/.dockerenv' exists). Every lock is taken over, with a warning, once
    it is 24 h old (stale_seconds); younger ones raise LockHeld. The pid
    can only shorten that wait, never lengthen it: when the lock was written
    on this host name and on the same side of the container boundary, a
    holder that no longer runs (os.kill(pid, 0) raises ProcessLookupError)
    is taken over at once with a message naming the dead pid and the lock's
    age. A live pid (or one we may not signal, PermissionError) is
    respected until the lock is stale, like any other; after 24 h it is
    taken for a reused pid. Our own pid, or one that cannot be checked,
    counts as unknown, and the lock is judged by age alone. A lock from
    another host name is judged by age alone too, since its pid means
    nothing here: a container run shows the container id as its host name
    and has a pid namespace of its own; so is a lock with the same host
    name written by a run inside a container, and a lock written on the
    host when this run is inside a container.

    The lock file is only ever created with O_CREAT | O_EXCL, so of several
    runs that find no lock, one creates it and the others raise LockHeld
    naming it. Takeovers are serialised by flock() on the regular file
    '.vidfunnel/lock.takeover' (else on the directory), which the kernel
    releases when its holder exits: inside it a run reads and judges the
    lock, checks that the file is still the one it judged (inode, mtime,
    size and content), unlinks it and creates its own with O_EXCL; a run
    that gets the flock after it then judges the new, live lock and raises
    LockHeld. Where the file system refuses flock() on both, the takeover
    runs without it and only that check and the O_EXCL create guard it (two
    racers can then both get in). The lock file is removed on exit, including
    on exception, if it is still ours.
    """

    def __init__(self, folder, warn=None, stale_seconds=LOCK_STALE_SECONDS):
        self.folder = Path(folder)
        self.path = record_dir(folder) / LOCK_NAME
        self.warn = warn if warn is not None else _warn
        self.stale_seconds = stale_seconds
        self.content = None

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
        """(inode, mtime in ns, size) of the lock file, or None when it does not exist."""
        try:
            status = os.stat(str(self.path))
        except FileNotFoundError:
            return None
        return (status.st_ino, status.st_mtime_ns, status.st_size)

    @contextlib.contextmanager
    def _takeover_guard(self):
        """Hold flock(LOCK_EX) while one takeover runs; see the class docstring.

        The flock is taken on the regular file '.vidfunnel/lock.takeover'
        (created if absent; it stays, empty), which more file systems support
        than a flock on a directory, NFS among them; the directory is the
        second choice. When neither works the takeover runs unguarded, with
        only the identity re-check and the O_EXCL create protecting it.
        """
        handle = None
        if fcntl is not None:
            targets = ((str(record_dir(self.folder) / TAKEOVER_FILE), os.O_RDWR | os.O_CREAT),
                       (str(record_dir(self.folder)), os.O_RDONLY))
            for target, flags in targets:
                try:
                    handle = os.open(target, flags, 0o644)
                    fcntl.flock(handle, fcntl.LOCK_EX)
                    break
                except OSError:
                    if handle is not None:
                        os.close(handle)
                    handle = None
        try:
            yield
        finally:
            if handle is not None:
                os.close(handle)  # closing the descriptor releases the flock

    def _create(self, content):
        """Create the lock file with our entry; FileExistsError when any lock file exists."""
        handle = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(_dump_json(content))

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
            holder, age = {}, 0.0
        return self._held(holder, age, "it took the lock over at the same time and got it first")

    def acquire(self):
        record_dir(self.folder).mkdir(parents=True, exist_ok=True)
        content = {"host": socket.gethostname(), "pid": os.getpid(), "started": now_iso(),
                   "container": in_container(), "machine": machine_id()}
        try:
            self._create(content)
        except FileExistsError:
            with self._takeover_guard():
                self._take_over(content)
        self.content = content
        return self

    def _take_over(self, content):
        """Judge the existing lock; take it over (unlink, then O_EXCL create) or raise LockHeld."""
        while True:
            judged = self._identity()
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
            liveness = None
            same_machine = content["machine"] is not None and holder.get("machine") == content["machine"]
            if (host == content["host"] and same_machine and holder_in_container == content["container"]
                    and pid != content["pid"]):
                liveness = pid_alive(pid)
            if liveness is False:
                warning = ("warning: taking over lock '{}': its holder, host '{}' pid {}, no longer runs "
                           "(lock written {} s ago)".format(self.path, host, pid, _whole_seconds(age)))
            elif age < self.stale_seconds:
                if liveness is True:
                    why = "its pid {} is running here; taken over once 24 h old".format(pid)
                elif host == content["host"] and not same_machine:
                    why = ("written on another machine with this host name (or before the machine id was "
                           "recorded), whose pid cannot be checked from here; taken over once 24 h old")
                else:
                    why = _unjudged_reason(host, holder_in_container, content, pid)
                raise self._held(holder, age, why)
            else:
                warning = "warning: taking over stale lock '{}' held by {} '{}' pid {} since {} ({:.1f} h ago{})".format(
                    self.path, holder_words, host, pid, holder.get("started"), age / 3600.0,
                    "; its pid is running here, taken for a reused one" if liveness is True else "")
            if self._identity() != judged or self._holder()[0] != holder:
                raise self._lost_race()
            try:
                os.unlink(str(self.path))
            except FileNotFoundError:
                pass
            try:
                self._create(content)
            except FileExistsError:
                raise self._lost_race()
            self.warn(warning)
            return

    def release(self):
        if self.content is None:
            return
        try:
            current = _read_json(self.path)
        except RecordError:
            current = None
        if current == self.content:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        else:
            self.warn("warning: lock '{}' no longer holds this run's entry; left in place".format(self.path))
        self.content = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
        return False

# End of File #
