"""Per-folder JSON records, fingerprints and the folder lock (HANDOFF.md section 6.7).

Each processed folder holds a hidden '.vidfunnel/' directory with one JSON record
per source file, named after the full source file name including its extension
('movie.avi.json'), plus 'folder.json' and, while a run is active, 'lock'.
Records are the authority; every write is atomic (temp file, then os.replace).
"""

VERSION = "20261109"

import contextlib
import datetime
import hashlib
import json
import os
import socket
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


FLOCK_RETRY_SECONDS = 1.0


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
        """Create the lock file, flock it, write our entry; FileExistsError when any lock file exists.

        Sets content["flock"] to whether the flock was taken and
        content["device"] to the file's st_dev, and keeps the descriptor open in self.handle and in the registry of held_lock_fds()
        until release(). A file whose entry could not be written is unlinked
        before its descriptor is closed.
        """
        handle = os.open(str(self.path), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            content["flock"] = _take_flock(handle)
            status = os.fstat(handle)
            content["device"] = status.st_dev
            data = _dump_json(content).encode("utf-8")
            while data:
                data = data[os.write(handle, data):]
        except BaseException:
            try:
                os.unlink(str(self.path))
            except OSError:
                pass
            os.close(handle)
            raise
        with _held_locks_guard:
            _held_locks[handle] = (status.st_dev, status.st_ino)
        self.handle = handle
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
            holder, age = {}, 0.0
        return self._held(holder, age, "it took the lock over at the same time and got it first")

    def acquire(self):
        record_dir(self.folder).mkdir(parents=True, exist_ok=True)
        machine, machine_source = machine_identity()
        content = {"host": socket.gethostname(), "pid": os.getpid(), "started": now_iso(),
                   "container": in_container(), "machine": machine, "machine_source": machine_source,
                   "boot": boot_id()}
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
        """Unlink the lock file if it is still ours, then close its descriptor (which frees our flock)."""
        if self.content is None:
            return
        handle = self.handle
        try:
            if handle is not None and self._still_ours():
                try:
                    os.unlink(str(self.path))
                except FileNotFoundError:
                    pass
            else:
                self.warn("warning: lock '{}' no longer holds this run's entry; left in place".format(self.path))
        finally:
            self.content = None
            self.handle = None
            if handle is not None:
                with _held_locks_guard:
                    _held_locks.pop(handle, None)
                    os.close(handle)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
        return False

# End of File #
