"""Per-folder JSON records, fingerprints and the folder lock (HANDOFF.md section 6.7).

Each processed folder holds a hidden '.vidfunnel/' directory with one JSON record
per source file, named after the full source file name including its extension
('movie.avi.json'), plus 'folder.json' and, while a run is active, 'lock'.
Records are the authority; every write is atomic (temp file, then os.replace).
"""

VERSION = "20261102"

import datetime
import hashlib
import json
import os
import socket
import sys
import tempfile
import uuid
from pathlib import Path

import vidfunnel

RECORD_DIR = ".vidfunnel"
LOCK_NAME = "lock"
FOLDER_FILE = "folder.json"
RECORD_SUFFIX = ".json"
STATUSES = ("planned", "converted", "verified", "quarantined", "failed", "retired", "source-changed")
STAGES = ("probe", "analyze", "plan", "convert", "verify", "retire")

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


class FolderLock:
    """Context manager holding '.vidfunnel/lock' for one folder.

    Raises LockHeld when another holder's lock is younger than 24 h; an older
    lock is taken over with a warning naming the stale holder. The lock file is
    removed on exit, including on exception, if it is still ours.
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
            return None, None
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

    def acquire(self):
        record_dir(self.folder).mkdir(parents=True, exist_ok=True)
        content = {"host": socket.gethostname(), "pid": os.getpid(), "started": now_iso()}
        text = _dump_json(content)
        try:
            handle = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            holder, age = self._holder()
            if holder is None:
                return self.acquire()
            host = holder.get("host")
            pid = holder.get("pid")
            if age < self.stale_seconds:
                raise LockHeld(
                    "folder '{}' is locked by host '{}' pid {} since {} ({:.0f} s ago); lock file '{}'".format(
                        self.folder, host, pid, holder.get("started"), age, self.path),
                    host=host, pid=pid, age_seconds=age)
            self.warn("warning: taking over stale lock '{}' held by host '{}' pid {} since {} ({:.1f} h ago)".format(
                self.path, host, pid, holder.get("started"), age / 3600.0))
            _write_json_atomic(self.path, content)
        else:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
        self.content = content
        return self

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
