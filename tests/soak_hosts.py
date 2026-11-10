#!/usr/bin/env python3
"""A multi-host soak test of vidfunnel's folder locks: simulated machines convert one library at once.

Not a unittest module (its name does not match test_*.py, so neither
'python3 -m unittest discover -s tests' nor tests/run_gate.sh runs it).
Run it from the repo root:

    python3 tests/soak_hosts.py
    python3 tests/soak_hosts.py --hosts 3 --scenarios freeze --rounds 2 --keep

What it does (HANDOFF.md section 9, "Multi-host soak"):

- Builds a small library with ffmpeg's lavfi sources (by default 6 folders
  of 1 to 4 clips and 5 loose clips in the library root, 20 clips of
  0.4 s in legacy containers) and plans it once with one host.
- For every scenario, copies the planned library (so each scenario starts
  fresh) and starts N vidfunnel processes on it, each posing as its own
  machine: its own host name (socket.gethostname), machine identity and
  boot id (vidfunnel.record.machine_identity and boot_id), so the
  cross-machine rules (heartbeat, takeover) decide and not the flock of the
  same kernel. The heartbeat constants are shortened in each host (beat
  every --beat s, stale after --stale s; a takeover-marker age constant
  named TAKEOVER*_SECONDS, when the tree has one, is set to --marker-stale).
- Each host runs real ffmpeg through a small wrapper of its own (its path,
  and so the host, is what the record's stages.convert.ffmpeg.path names).
  The wrapper creates the part file itself before ffmpeg opens it, keeps a
  descriptor to it, and logs one line on start and on exit (host, pid,
  wall time in ns, output path, inode, size, SHA-256 of what this ffmpeg
  wrote, exit status) to the scenario's shared event log, then holds the
  encode open for --delay s so that runs overlap.
- Inside each host an audit hook logs every write-like file operation under
  the library (open for writing, rename, remove, mkdir, utime, ...), and the
  folder lock's acquire and release are logged, so every write can be
  attributed to a host and checked against the locks it held.

Scenarios (each with N hosts, N from --hosts):
  plain      all hosts run to completion.
  kill       one host's process group gets SIGKILL while it encodes in a
             folder it holds; the others go on; once the lock is older than
             the stale age, the survivors run convert again and one of them
             takes the folder over and finishes it.
  freeze     one host's process group gets SIGSTOP while it encodes in a
             folder it holds and stays stopped until another host has taken
             the folder over; then SIGCONT. It must lose the lock and write
             nothing more in that folder but the removal of its own attempt's
             part file; its encode's result is discarded.
  staggered  hosts start --stagger s apart; the first one to finish is run
             again while the others still work.

Checks after each scenario: one host per folder at a time (lock holds and
encodes), every write inside a lock its host held, no two encodes holding one
file (by name or by inode) at the same time, one accepted encode per
file (the final output's inode is the one that encode wrote, the record names
the same host), everything converted and verified, a final 'vidfunnel
verify' that passes, no part, lock or takeover leftovers, no traceback, the
expected exit codes, no process left in any host's process group, and (for
plain) that two hosts really encoded in different folders at the same time.

Internal modes: '--as-host NAME ... -- VIDFUNNEL-ARGS' runs one vidfunnel
command posing as host NAME (the soak starts these itself; PYTHONPATH must
name the repo root). --sabotage-locks (hidden) makes the second host's
FolderLock.acquire succeed without creating a lock: the negative control,
which must make the soak report FAIL. --hold-before-open moves the wrapper's
wait to before the part file is made and ffmpeg starts, so a freeze lands in
the window between convert's part-file cleanup and the encoder's own open of
the part file (a probe of that window, not part of the default run);
--resume-after-cleanup sends the SIGCONT only once the new holder has started
its own encode of the frozen host's file.
"""

VERSION = "20261110.1"

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.abspath(__file__)

HOST_NAMES = ("harbour", "meadow", "orchard", "valley", "lantern", "thistle")
FOLDER_NAMES = ("alder", "birch", "cedar", "hazel", "larch", "rowan", "spruce", "willow", "poplar", "aspen")
CLIP_WORDS = ("amber", "basil", "coral", "dune", "ember", "fennel", "garnet", "heron", "iris", "juniper",
              "kestrel", "lichen", "maple", "nettle", "otter", "pebble", "quartz", "reed", "sorrel", "tansy",
              "umber", "violet", "wren", "yarrow", "zinnia", "acorn", "bramble", "clover", "daisy", "elm")
FOLDER_SIZES = (4, 3, 2, 1, 3, 2)   # clips per folder, repeated for more folders
SCENARIOS = ("plain", "kill", "freeze", "staggered")
MARKER = ".vidfunnel-soak-library"
RECORD_DIR = ".vidfunnel"
POLL_SECONDS = 0.05
STILL_WAITING_EVERY = 10.0

# Files in '.vidfunnel/' that a run writes while it takes or releases a lock: written before its
# acquire returns or by its release, so they are not held to the "inside a lock this run holds" rule.
LOCK_MACHINERY_NAMES = ("lock", "lock.takeover", "lock.taking")
LOCK_MACHINERY_PREFIXES = ("lock.new.", "lock.aside.", "lock.probe.")
# Of those, the ones that must not remain once every host has exited ('lock.takeover' stays by design).
LEFTOVER_NAMES = ("lock", "lock.taking")
LEFTOVER_PREFIXES = ("lock.new.", "lock.aside.", "lock.probe.")

# Clip recipes: (what, extension, codec arguments). Each clip is 0.4 s of testsrc and a sine tone whose
# frequency differs per clip, so no two sources are the same file.
RECIPES = (
    ("MPEG-4 ASP in AVI", ".avi", "160x120",
     ["-c:v", "mpeg4", "-vtag", "XVID", "-q:v", "5", "-bf", "0", "-c:a", "libmp3lame", "-b:a", "64k", "-ac", "2",
      "-ar", "44100", "-f", "avi"]),
    ("MPEG-2 in MPEG-PS", ".mpg", "352x288",
     ["-aspect", "4:3", "-c:v", "mpeg2video", "-b:v", "2000k", "-g", "15", "-bf", "2", "-pix_fmt", "yuv420p",
      "-c:a", "ac3", "-b:a", "128k", "-ac", "2", "-ar", "48000", "-f", "dvd"]),
    ("FLV1 in FLV", ".flv", "160x120",
     ["-c:v", "flv", "-q:v", "5", "-c:a", "libmp3lame", "-b:a", "64k", "-ac", "2", "-ar", "44100", "-f", "flv"]),
    ("H.264 in MKV", ".mkv", "160x120",
     ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k",
      "-ac", "2", "-ar", "48000", "-f", "matroska"]),
    ("WMV2 in ASF", ".wmv", "160x120",
     ["-c:v", "wmv2", "-b:v", "300k", "-c:a", "wmav2", "-b:a", "64k", "-ac", "2", "-ar", "44100", "-f", "asf"]),
)
CLIP_SECONDS = "0.4"


# ---------------------------------------------------------------- the ffmpeg wrapper

WRAPPER = r'''#!{python}
"""ffmpeg wrapper of soak host {host!r}: logs encodes to the soak's event log; written by tests/soak_hosts.py."""
import hashlib
import json
import os
import signal
import subprocess
import sys
import time

CONFIG = {config}


def log(kind, **fields):
    fields.update(t=time.time_ns(), kind=kind, host=CONFIG["host"], pid=os.getpid(), ppid=os.getppid())
    line = (json.dumps(fields, sort_keys=True) + "\n").encode("utf-8")
    handle = os.open(CONFIG["events"], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(handle, line)
    finally:
        os.close(handle)


argv = sys.argv[1:]
output = argv[-1] if argv else ""
if not output.endswith(".part"):
    os.execv(CONFIG["ffmpeg"], [CONFIG["ffmpeg"]] + argv)

output = os.path.abspath(output)
held = None
inode = None
if CONFIG["where"] == "before":
    # The probe of the moment before ffmpeg opens its output: the encode waits here, its part file not yet made.
    log("ffmpeg-start", output=output, inode=None, created="later")
    if CONFIG["delay"] > 0:
        time.sleep(CONFIG["delay"])
try:
    held = os.open(output, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644)
    inode = os.fstat(held).st_ino
    created = "created"
except FileExistsError:
    created = "existed"
except OSError as error:
    created = "error: {{}}".format(error)
log("ffmpeg-open" if CONFIG["where"] == "before" else "ffmpeg-start", output=output, inode=inode, created=created)
child = subprocess.Popen([CONFIG["ffmpeg"]] + argv, close_fds=False)


def forward(signum, frame):
    try:
        child.send_signal(signum)
    except OSError:
        pass


for name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT"):
    signal.signal(getattr(signal, name), forward)
status = child.wait()
size = None
digest = None
if held is not None:
    try:
        size = os.fstat(held).st_size
        os.lseek(held, 0, os.SEEK_SET)
        sha = hashlib.sha256()
        while True:
            chunk = os.read(held, 1 << 20)
            if not chunk:
                break
            sha.update(chunk)
        digest = sha.hexdigest()
    except OSError:
        pass
if status == 0 and CONFIG["delay"] > 0 and CONFIG["where"] == "after":
    time.sleep(CONFIG["delay"])
log("ffmpeg-exit", output=output, inode=inode, size=size, sha256=digest, status=status)
sys.exit(status if status >= 0 else 128 - status)
'''


def write_wrapper(directory, host, events, real_ffmpeg, real_ffprobe, delay, where="after"):
    """The host's own 'ffmpeg' (the wrapper) and 'ffprobe' (a link to the real one) in `directory`.

    where: 'after' holds each encode open for `delay` s after ffmpeg has ended (its output written and
    still open in the wrapper); 'before' waits `delay` s before the part file is made and ffmpeg starts.
    """
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "ffmpeg")
    config = {"host": host, "events": events, "ffmpeg": real_ffmpeg, "delay": delay, "where": where}
    text = WRAPPER.format(python=sys.executable, host=host, config=repr(config))
    # Rewritten only when it changes (a new scenario's event log), so vidfunnel's capability cache stays valid
    # for the runs of one scenario.
    if read_text(path) != text:
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(path, 0o755)
    probe = os.path.join(directory, "ffprobe")
    if not os.path.islink(probe) or os.readlink(probe) != real_ffprobe:
        if os.path.lexists(probe):
            os.unlink(probe)
        os.symlink(real_ffprobe, probe)
    return path


def read_text(path):
    try:
        with open(path, encoding="utf-8") as stream:
            return stream.read()
    except OSError:
        return None


# ---------------------------------------------------------------- host mode (one posing vidfunnel process)

class EventLog(object):
    """Appends one JSON line per event with a single os.write on an O_APPEND descriptor."""

    def __init__(self, path, host):
        self.host = host
        self.handle = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)

    def write(self, kind, **fields):
        fields.update(t=time.time_ns(), kind=kind, host=self.host, pid=os.getpid())
        os.write(self.handle, (json.dumps(fields, sort_keys=True) + "\n").encode("utf-8"))


AUDITED = {"open", "os.rename", "os.remove", "os.rmdir", "os.mkdir", "os.utime", "os.truncate", "os.chmod",
           "os.chown", "os.link", "os.symlink", "shutil.rmtree", "shutil.copyfile", "shutil.move", "os.setxattr",
           "os.removexattr"}


def _path_text(value):
    if isinstance(value, int) or value is None:
        return None
    try:
        return os.path.abspath(os.fsdecode(os.fspath(value)))
    except TypeError:
        return None


def install_write_audit(events, watch_root):
    """Log every write-like file operation of this process on a path under `watch_root` (sys.addaudithook)."""
    root = os.path.abspath(watch_root) + os.sep
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def hook(event, args):
        if event not in AUDITED:
            return
        try:
            if event == "open":
                mode, flags = args[1], args[2]
                writes = (isinstance(flags, int) and flags & write_flags) or (
                    isinstance(mode, str) and any(letter in mode for letter in "wax+"))
                if not writes:
                    return
                paths = [_path_text(args[0])]
            elif event in ("os.rename", "os.link", "os.symlink", "shutil.copyfile", "shutil.move"):
                paths = [_path_text(args[0]), _path_text(args[1])]
            else:
                paths = [_path_text(args[0])]
            paths = [path for path in paths if path is not None and (path + os.sep).startswith(root)]
            if paths:
                events.write("fs", op=event, paths=paths)
        except Exception:
            pass

    sys.addaudithook(hook)


def host_main(options, vidfunnel_args):
    """Run one vidfunnel command posing as machine `options.as_host`; returns its exit code."""
    try:
        import socket
        import vidfunnel.record as record
        from vidfunnel.cli import main as vidfunnel_main
    except ImportError as error:
        sys.stderr.write("soak_hosts: cannot import vidfunnel ({}); set PYTHONPATH to the repo root '{}'\n".format(
            error, REPO_ROOT))
        return 2
    name = options.as_host
    pose = "soak-" + name
    events = EventLog(options.events, name)
    record.machine_identity = lambda: (pose + "-machine", "machine-id")
    record.boot_id = lambda: pose + "-boot"
    socket.gethostname = lambda: pose
    settings = {"HEARTBEAT_SECONDS": options.beat, "HEARTBEAT_STALE_SECONDS": options.stale}
    for attribute in dir(record):
        if attribute.startswith("TAKEOVER") and attribute.endswith("_SECONDS"):
            settings[attribute] = options.marker_stale
    for item in options.set or ():
        key, _, value = item.partition("=")
        settings[key] = float(value)
    for key, value in sorted(settings.items()):
        if not hasattr(record, key):
            sys.stderr.write("soak_hosts: vidfunnel.record has no constant '{}'\n".format(key))
            return 2
        setattr(record, key, int(value) if float(value).is_integer() else float(value))

    lock_class = record.FolderLock
    original_acquire = lock_class.acquire
    original_release = lock_class.release

    def folder_of(lock):
        return os.path.abspath(os.fspath(lock.folder))

    def acquire(self):
        if options.sabotage:
            # The negative control: report the lock as taken without creating it.
            events.write("lock-acquired", folder=folder_of(self))
            self._soak_held = True
            return self
        try:
            result = original_acquire(self)
        except record.LockHeld as error:
            events.write("lock-refused", folder=folder_of(self), reason=str(error))
            raise
        except OSError as error:
            events.write("lock-error", folder=folder_of(self), reason=str(error))
            raise
        events.write("lock-acquired", folder=folder_of(self))
        self._soak_held = True
        return result

    def release(self):
        if getattr(self, "_soak_held", False):
            self._soak_held = False
            events.write("lock-release", folder=folder_of(self))
        if options.sabotage:
            return None
        return original_release(self)

    lock_class.acquire = acquire
    lock_class.release = release
    install_write_audit(events, options.watch)
    events.write("host-start", argv=vidfunnel_args, pose=pose, settings=settings, sabotage=bool(options.sabotage))
    code = "exception"
    try:
        code = vidfunnel_main(vidfunnel_args)
        return code
    finally:
        events.write("host-end", code=code)


# ---------------------------------------------------------------- driver helpers

class ScenarioError(Exception):
    """A scenario could not be carried out (a deadline passed, a setup condition never came)."""


def say(text):
    print(text, flush=True)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return None


def sha256_of(path):
    sha = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def walk_folders(library):
    """Every folder of the library (the root included), skipping '.vidfunnel' directories."""
    found = []
    for directory, subdirectories, _files in os.walk(library):
        subdirectories[:] = sorted(entry for entry in subdirectories if entry != RECORD_DIR)
        found.append(directory)
    return found


def read_records(library):
    """{source path: record dict} for every record under the library; unreadable ones map to None."""
    records = {}
    for folder in walk_folders(library):
        directory = os.path.join(folder, RECORD_DIR)
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if name.endswith(".json") and name != "folder.json" and not name.startswith("."):
                records[os.path.join(folder, name[:-len(".json")])] = read_json(os.path.join(directory, name))
    return records


def folder_of_path(path):
    """The library folder a written path belongs to: '.vidfunnel/' entries and '.vidfunnel' itself count for theirs."""
    parent = os.path.dirname(path)
    if os.path.basename(path) == RECORD_DIR:
        return parent
    if os.path.basename(parent) == RECORD_DIR:
        return os.path.dirname(parent)
    return parent


# A convert part file: '.NAME.<id8>.<token>.part' (one per attempt, 20261110.1 on) or '.NAME.<id8>.part' (before).
PART_NAME = re.compile(r"(\..+\.[0-9a-f]{8})(?:\.[0-9a-f]{12})?\.part")


def record_part_key(path):
    """The part path of `path` without its attempt token: the same for every attempt on one record's output."""
    match = PART_NAME.fullmatch(os.path.basename(path))
    return os.path.join(os.path.dirname(path), match.group(1)) if match else path


def is_lock_machinery(path):
    """True for '.vidfunnel' itself and the lock files a run writes while it takes or releases a lock."""
    name = os.path.basename(path)
    if name == RECORD_DIR:
        return True
    if os.path.basename(os.path.dirname(path)) != RECORD_DIR:
        return False
    return name in LOCK_MACHINERY_NAMES or name.startswith(LOCK_MACHINERY_PREFIXES)


def short(path, library):
    relative = os.path.relpath(path, library)
    return "(root)" if relative == "." else relative


class HostRun(object):
    """One vidfunnel process posing as a host."""

    def __init__(self, host, label, command, log_path, popen):
        self.host = host
        self.label = label        # 'harbour #2' for the second run of harbour in a scenario
        self.command = command
        self.log_path = log_path
        self.popen = popen
        self.pid = popen.pid
        self.started_ns = time.time_ns()
        self.ended_ns = None
        self.code = None
        self.killed = False
        self.stopped_ns = None    # SIGSTOP sent
        self.continued_ns = None  # SIGCONT sent
        self.gone_ns = None       # the process group is empty after a SIGKILL

    def poll(self):
        if self.code is None:
            code = self.popen.poll()
            if code is not None:
                self.code = code
                self.ended_ns = time.time_ns()
        return self.code

    def alive(self):
        return self.poll() is None

    def output(self):
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as stream:
                return stream.read()
        except OSError:
            return ""


# ---------------------------------------------------------------- the soak

class Soak(object):

    def __init__(self, options):
        self.options = options
        self.real_ffmpeg = shutil.which(options.real_ffmpeg) if os.sep not in options.real_ffmpeg \
            else options.real_ffmpeg
        if not self.real_ffmpeg or not os.access(self.real_ffmpeg, os.X_OK):
            raise SystemExit("soak_hosts: ffmpeg '{}' was not found".format(options.real_ffmpeg))
        self.real_ffprobe = shutil.which("ffprobe", path=os.path.dirname(self.real_ffmpeg)) or shutil.which("ffprobe")
        if not self.real_ffprobe:
            raise SystemExit("soak_hosts: ffprobe was not found next to '{}' nor on PATH".format(self.real_ffmpeg))
        if options.work:
            self.work = os.path.abspath(options.work)
            os.makedirs(self.work, exist_ok=True)
        else:
            self.work = tempfile.mkdtemp(prefix="vidfunnel-soak.")
        self.area = os.path.abspath(options.library) if options.library else os.path.join(self.work, "libraries")
        self.cache = os.path.join(self.work, "cache")
        self.live = []        # every HostRun not yet known to be over, for the cleanup
        self.template = None  # the planned library every scenario copies
        self.source = None    # the same library before the plan (--plan-each copies this one)
        self.clips = 0

    # -- the library area

    def prepare_area(self):
        """Create or reuse the library area; refuse a non-empty folder this script did not mark as its own."""
        area = self.area
        marker = os.path.join(area, MARKER)
        if os.path.isdir(area) and os.listdir(area) and not os.path.isfile(marker):
            raise SystemExit("soak_hosts: '{}' is not empty and has no '{}' marker: refusing to use it (the soak only "
                             "works in an empty folder or one it made itself)".format(area, MARKER))
        if os.path.exists(area) and not os.path.isdir(area):
            raise SystemExit("soak_hosts: '{}' is not a folder".format(area))
        os.makedirs(area, exist_ok=True)
        if not os.path.isfile(marker):
            with open(marker, "w", encoding="utf-8") as stream:
                stream.write("made by vidfunnel tests/soak_hosts.py {}; everything in this folder may be deleted "
                             "by it\n".format(VERSION))
        for name in ("template", "runs"):
            path = os.path.join(area, name)
            if os.path.isdir(path):
                say("removing the previous soak's '{}'".format(path))
                shutil.rmtree(path)
        os.makedirs(os.path.join(area, "runs"))

    def build_template(self):
        """Make the clips in '<area>/template/source' with lavfi; returns the number of clips."""
        source = os.path.join(self.area, "template", "source")
        os.makedirs(source)
        folders = [FOLDER_NAMES[index % len(FOLDER_NAMES)] + ("" if index < len(FOLDER_NAMES) else str(index))
                   for index in range(self.options.folders)]
        plan = [(os.path.join(source, folder), FOLDER_SIZES[index % len(FOLDER_SIZES)])
                for index, folder in enumerate(folders)]
        plan.append((source, self.options.loose))
        total = sum(count for _folder, count in plan)
        say("building a library of {} clips ({} folders and {} loose clips) in '{}'".format(
            total, len(folders), self.options.loose, source))
        started = time.monotonic()
        number = 0
        for folder, count in plan:
            os.makedirs(folder, exist_ok=True)
            for _ in range(count):
                what, extension, size, codec = RECIPES[number % len(RECIPES)]
                stem = CLIP_WORDS[number % len(CLIP_WORDS)] + ("" if number < len(CLIP_WORDS) else str(number))
                target = os.path.join(folder, stem + extension)
                frequency = 300 + 37 * number
                command = [self.real_ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                           "-f", "lavfi", "-i", "testsrc=size={}:rate=25:duration={}".format(size, CLIP_SECONDS),
                           "-f", "lavfi", "-i", "sine=frequency={}:sample_rate=48000:duration={}".format(
                               frequency, CLIP_SECONDS)] + codec + [target]
                done = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, universal_newlines=True)
                if done.returncode != 0 or not os.path.isfile(target):
                    raise SystemExit("soak_hosts: ffmpeg could not make '{}' ({}): {}".format(
                        target, what, done.stdout.strip()[-400:]))
                number += 1
            say("  {}: {} clip(s)".format(short(folder, source), count))
        say("built {} clips in {:.1f} s".format(number, time.monotonic() - started))
        return source, number

    # -- hosts

    def wrapper_dir(self, host):
        return os.path.join(self.work, "hosts", host)

    def host_of_ffmpeg_path(self, path):
        """The host whose wrapper `path` is, or None."""
        if not isinstance(path, str):
            return None
        directory = os.path.dirname(path)
        if os.path.dirname(directory) == os.path.join(self.work, "hosts"):
            return os.path.basename(directory)
        return None

    def start_host(self, scene, host, vidfunnel_args, sabotage=False):
        count = sum(1 for run in scene.runs if run.host == host) + 1
        label = "{} #{}".format(host, count)
        log_path = os.path.join(scene.logs, "{}-{}.log".format(host, count))
        wrapper = write_wrapper(self.wrapper_dir(host), host, scene.events, self.real_ffmpeg, self.real_ffprobe,
                                self.options.delay, "before" if self.options.hold_before_open else "after")
        command = [sys.executable, SCRIPT, "--as-host", host, "--events", scene.events, "--watch", self.area,
                   "--beat", str(self.options.beat), "--stale", str(self.options.stale),
                   "--marker-stale", str(self.options.marker_stale)]
        for item in self.options.set or ():
            command += ["--set", item]
        if sabotage:
            command.append("--sabotage")
        command += ["--", "--ffmpeg", wrapper] + list(vidfunnel_args)
        environment = dict(os.environ)
        environment["PYTHONPATH"] = REPO_ROOT
        environment["XDG_CACHE_HOME"] = self.cache
        environment["PYTHONPYCACHEPREFIX"] = os.path.join(self.work, "pycache")
        environment.pop("VIDFUNNEL_FFMPEG", None)
        log = open(log_path, "wb")
        try:
            popen = subprocess.Popen(command, cwd=scene.logs, stdin=subprocess.DEVNULL, stdout=log,
                                     stderr=subprocess.STDOUT, env=environment, start_new_session=True)
        finally:
            log.close()
        run = HostRun(host, label, command, log_path, popen)
        scene.runs.append(run)
        self.live.append(run)
        say("  started {} (pid {}): vidfunnel {}{}".format(label, run.pid, " ".join(vidfunnel_args[:-1]),
                                                           " [locks sabotaged]" if sabotage else ""))
        return run

    def cleanup(self):
        """Stop every host process group still alive (SIGCONT first, so a stopped one can die)."""
        for run in self.live:
            if group_alive(run.pid):
                for signum in (signal.SIGCONT, signal.SIGKILL):
                    try:
                        os.killpg(run.pid, signum)
                    except OSError:
                        pass
            try:
                run.popen.wait(10)
            except subprocess.TimeoutExpired:
                pass
        self.live = []

    # -- waiting

    def wait_until(self, condition, what, deadline=None):
        """Poll `condition` until true; ScenarioError naming `what` when the deadline passes first."""
        deadline = self.options.deadline if deadline is None else deadline
        started = time.monotonic()
        next_note = started + STILL_WAITING_EVERY
        while True:
            value = condition()
            if value:
                return value
            now = time.monotonic()
            if now - started > deadline:
                raise ScenarioError("waited {:.0f} s for {}; it never came".format(deadline, what))
            if now >= next_note:
                say("    still waiting for {} ({:.0f} s)".format(what, now - started))
                next_note = now + STILL_WAITING_EVERY
            time.sleep(POLL_SECONDS)

    def wait_runs(self, scene, runs, what):
        """Wait until every run in `runs` has exited and its process group is empty."""
        try:
            self.wait_until(lambda: all(run.poll() is not None for run in runs), what)
        except ScenarioError:
            for run in runs:
                if run.alive():
                    scene.fail("{} was still running after {:.0f} s (pid {}); killed".format(
                        run.label, self.options.deadline, run.pid))
            raise
        for run in runs:
            self.check_group_empty(scene, run)

    def check_group_empty(self, scene, run):
        if run.gone_ns is not None:
            return
        try:
            self.wait_until(lambda: not group_alive(run.pid), "the process group of {} to empty".format(run.label),
                            deadline=15)
        except ScenarioError:
            scene.fail("{} exited but its process group {} still has a process (an encoder left running?)".format(
                run.label, run.pid))
            return
        run.gone_ns = time.time_ns()

    def signal_group(self, scene, run, signum):
        os.killpg(run.pid, signum)
        now = time.time_ns()
        scene.log_driver(signal.Signals(signum).name, target=run.host, target_pid=run.pid)
        return now

    def beat_age(self, lock_path):
        """Seconds since the lock file's last beat on the file system's clock (a probe file in the area)."""
        probe = os.path.join(self.area, ".soak-clock-" + uuid.uuid4().hex)
        handle = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            now = os.fstat(handle).st_mtime_ns
        finally:
            os.close(handle)
            os.unlink(probe)
        try:
            return (now - os.stat(lock_path).st_mtime_ns) / 1e9
        except FileNotFoundError:
            return None

    # -- one scenario

    def run_scenario(self, scenario, count, round_number):
        tag = "{}-{}hosts-round{}".format(scenario, count, round_number)
        scene = Scene(self, tag, scenario, HOST_NAMES[:count])
        say("")
        say("== {}: {} hosts ({}), round {} ==".format(scenario, count, ", ".join(scene.hosts), round_number))
        started = time.monotonic()
        try:
            scene.prepare()
            getattr(self, "scenario_" + scenario)(scene)
            scene.finish()
        except ScenarioError as error:
            scene.fail(str(error))
        finally:
            self.cleanup()
        try:
            scene.check()
        except Exception as error:  # a broken check is a failure of the soak itself, reported as such
            scene.fail("the checks themselves failed: {}: {}".format(type(error).__name__, error))
        scene.elapsed = time.monotonic() - started
        return scene

    def scenario_plain(self, scene):
        runs = [self.start_host(scene, host, scene.convert_args(),
                                sabotage=self.options.sabotage_locks and index == 1)
                for index, host in enumerate(scene.hosts)]
        self.wait_runs(scene, runs, "every host to finish")

    def _encode_in_flight(self, scene, run):
        """(folder, start event) of an encode `run` has started and not ended, in a folder whose lock names it."""
        def look():
            started = {}
            for event in scene.events_now():
                if event.get("kind") == "ffmpeg-start" and event.get("ppid") == run.pid:
                    started[event["pid"]] = event
                elif event.get("kind") == "ffmpeg-exit" and event.get("ppid") == run.pid:
                    started.pop(event["pid"], None)
            for event in started.values():
                folder = os.path.dirname(event["output"])
                holder = read_json(os.path.join(folder, RECORD_DIR, "lock"))
                if isinstance(holder, dict) and holder.get("host") == scene.pose(run.host):
                    return folder, event
            if not run.alive():
                raise ScenarioError("{} ended before it was caught encoding (exit {})".format(run.label, run.code))
            return None
        return self.wait_until(look, "{} to be encoding in a folder it holds".format(run.label))

    def _survivors_pass(self, scene, survivors):
        runs = [self.start_host(scene, host, scene.convert_args()) for host in survivors]
        return runs

    def _wait_stale(self, scene, folder, victim):
        lock = os.path.join(folder, RECORD_DIR, "lock")
        stale = self.options.stale + 1.0

        def silent():
            holder = read_json(lock)
            if not isinstance(holder, dict) or holder.get("host") != scene.pose(victim.host):
                return True  # gone, or someone else's now
            age = self.beat_age(lock)
            return age is None or age > stale
        say("    waiting until the lock of '{}' has been silent for more than {:.0f} s".format(
            short(folder, scene.library), stale))
        self.wait_until(silent, "the lock of '{}' to go stale".format(short(folder, scene.library)))

    def scenario_kill(self, scene):
        survivors = scene.hosts[1:]
        runs = [self.start_host(scene, host, scene.convert_args()) for host in scene.hosts]
        victim = runs[0]
        folder, event = self._encode_in_flight(scene, victim)
        say("    SIGKILL to the process group of {} while it encodes '{}' in '{}'".format(
            victim.label, os.path.basename(event["output"]), short(folder, scene.library)))
        scene.victim, scene.victim_folder, scene.victim_encode = victim, folder, event["pid"]
        victim.stopped_ns = self.signal_group(scene, victim, signal.SIGKILL)
        victim.killed = True
        self.wait_until(lambda: victim.poll() is not None and not group_alive(victim.pid),
                        "the process group of {} to die".format(victim.label))
        victim.gone_ns = time.time_ns()
        self.wait_runs(scene, runs[1:], "the surviving hosts to finish their first pass")
        self._wait_stale(scene, folder, victim)
        say("    the survivors run convert again")
        second = self._survivors_pass(scene, survivors)
        self.wait_runs(scene, second, "the surviving hosts' second pass")

    def scenario_freeze(self, scene):
        survivors = scene.hosts[1:]
        runs = [self.start_host(scene, host, scene.convert_args()) for host in scene.hosts]
        victim = runs[0]
        folder, event = self._encode_in_flight(scene, victim)
        say("    SIGSTOP to the process group of {} while it encodes '{}' in '{}'".format(
            victim.label, os.path.basename(event["output"]), short(folder, scene.library)))
        scene.victim, scene.victim_folder, scene.victim_encode = victim, folder, event["pid"]
        victim.stopped_ns = self.signal_group(scene, victim, signal.SIGSTOP)
        current = runs[1:]
        passes = 1

        def taken():
            for item in scene.events_now():
                if (item.get("kind") == "lock-acquired" and item.get("folder") == folder
                        and item.get("host") != victim.host and item["t"] > victim.stopped_ns):
                    return item
            holder = read_json(os.path.join(folder, RECORD_DIR, "lock"))
            if isinstance(holder, dict) and holder.get("host") not in (None, scene.pose(victim.host)):
                return {"host": holder.get("host")}
            return None

        while True:
            self.wait_until(lambda: taken() or all(run.poll() is not None for run in current),
                                    "another host to take '{}' over, or the survivors to finish".format(
                                        short(folder, scene.library)))
            if taken():
                break
            for run in current:
                self.check_group_empty(scene, run)
            if passes >= 3:
                raise ScenarioError("{} passes of the survivors did not take '{}' over from the stopped {}".format(
                    passes, short(folder, scene.library), victim.label))
            self._wait_stale(scene, folder, victim)
            say("    the survivors run convert again (pass {})".format(passes + 1))
            current = self._survivors_pass(scene, survivors)
            passes += 1
        holder = taken()
        if self.options.resume_after_cleanup:
            part = event["output"]

            def restarted():
                return any(item.get("kind") == "ffmpeg-start"
                           and record_part_key(item.get("output") or "") == record_part_key(part)
                           and item.get("ppid") != victim.pid for item in scene.events_now())
            self.wait_until(restarted, "another host to start its own encode of '{}'".format(short(part, scene.library)))
        say("    '{}' taken over by {}; SIGCONT to {}".format(short(folder, scene.library), holder.get("host"),
                                                             victim.label))
        victim.continued_ns = self.signal_group(scene, victim, signal.SIGCONT)
        self.wait_runs(scene, [run for run in scene.runs], "every host to finish after the SIGCONT")

    def scenario_staggered(self, scene):
        runs = []
        first_start = time.monotonic()
        for index, host in enumerate(scene.hosts):
            if index:
                start_at = first_start + index * self.options.stagger
                self.wait_until(lambda: time.monotonic() >= start_at, "the start time of {}".format(host),
                                deadline=self.options.stagger * len(scene.hosts) + 10)
            runs.append(self.start_host(scene, host, scene.convert_args()))
        done = self.wait_until(lambda: [run for run in runs if run.poll() is not None],
                               "the first host to finish")[0]
        self.check_group_empty(scene, done)
        others = [run for run in runs if run is not done and run.alive()]
        if not others:
            scene.fail("the first host to finish ({}) finished after every other host, so no re-run could overlap "
                       "their work (raise --delay or --stagger)".format(done.label))
        else:
            say("    {} finished while {} still work(s); running it again".format(
                done.label, ", ".join(run.label for run in others)))
        scene.rerun = self.start_host(scene, done.host, scene.convert_args())
        scene.rerun_overlapped = [run.label for run in others if run.alive()]
        self.wait_runs(scene, list(scene.runs), "every host to finish")

    # -- the whole matrix

    def plan_template(self):
        source, clips = self.build_template()
        self.source = source
        self.clips = clips
        planned = os.path.join(self.area, "template", "planned")
        shutil.copytree(source, planned, copy_function=shutil.copy2)
        scene = Scene(self, "template-plan", "plan", HOST_NAMES[:1])
        scene.library = planned
        scene.prepare_logs()
        say("planning the library once, as host {}".format(HOST_NAMES[0]))
        started = time.monotonic()
        run = self.start_host(scene, HOST_NAMES[0], ["plan", planned])
        try:
            self.wait_runs(scene, [run], "the plan of the template library")
        finally:
            self.cleanup()
        text = run.output()
        records = read_records(planned)
        statuses = sorted(set((record or {}).get("status") for record in records.values()))
        if run.code != 0 or "Traceback" in text or len(records) != clips or statuses != ["planned"]:
            raise SystemExit("soak_hosts: the plan of the template did not give {} planned records (exit {}, {} "
                             "records, statuses {}); log '{}':\n{}".format(clips, run.code, len(records), statuses,
                                                                           run.log_path, text[-3000:]))
        say("planned {} clips in {:.1f} s".format(clips, time.monotonic() - started))
        self.template = planned


class Scene(object):
    """One scenario run: its library, logs, event log, runs and failures."""

    def __init__(self, soak, tag, scenario, hosts):
        self.soak = soak
        self.tag = tag
        self.scenario = scenario
        self.hosts = list(hosts)
        self.runs = []
        self.failures = []
        self.notes = []
        self.counts = {}
        self.library = os.path.join(soak.area, "runs", tag)
        self.logs = os.path.join(soak.work, "logs", tag)
        self.events = os.path.join(self.logs, "events.jsonl")
        self.victim = None
        self.victim_folder = None
        self.victim_encode = None
        self.rerun = None
        self.rerun_overlapped = []
        self.verify_run = None
        self.elapsed = 0.0
        self._driver_log = None

    def pose(self, host):
        return "soak-" + host

    def fail(self, message):
        self.failures.append(message)

    def prepare_logs(self):
        os.makedirs(self.logs, exist_ok=True)
        self._driver_log = EventLog(self.events, "driver")

    def prepare(self):
        self.prepare_logs()
        if self.soak.options.plan_each:
            shutil.copytree(self.soak.source, self.library, copy_function=shutil.copy2)
            run = self.soak.start_host(self, self.hosts[0], ["plan", self.library])
            self.soak.wait_runs(self, [run], "the plan of this scenario's library")
        else:
            shutil.copytree(self.soak.template, self.library, copy_function=shutil.copy2)
        self.planned = {source: record for source, record in read_records(self.library).items()}
        bad = [source for source, record in self.planned.items() if (record or {}).get("status") != "planned"]
        if bad or not self.planned:
            raise ScenarioError("the fresh library has records that are not planned: {}".format(bad))
        say("  library '{}': {} planned records".format(self.library, len(self.planned)))

    def log_driver(self, kind, **fields):
        self._driver_log.write(kind, **fields)

    def convert_args(self):
        return ["convert", "--jobs", str(self.soak.options.jobs), self.library]

    def events_now(self):
        """Every complete line of the event log so far."""
        events = []
        try:
            with open(self.events, encoding="utf-8") as stream:
                for line in stream:
                    if line.endswith("\n"):
                        try:
                            events.append(json.loads(line))
                        except ValueError:
                            pass
        except OSError:
            pass
        return events

    def finish(self):
        """After the scenario's own steps: the final verify by one host still standing."""
        survivors = [host for host in self.hosts if self.victim is None or host != self.victim.host
                     or self.scenario == "freeze"]
        host = survivors[-1]
        say("  verifying the library as {}".format(host))
        self.verify_run = self.soak.start_host(self, host, ["verify", self.library])
        self.soak.wait_runs(self, [self.verify_run], "the final verify")

    # -- the checks

    def check(self):
        options = self.soak.options
        lines = []
        try:
            with open(self.events, encoding="utf-8") as stream:
                lines = stream.read().split("\n")
        except OSError as error:
            self.fail("the event log '{}' cannot be read: {}".format(self.events, error))
        events = []
        for number, line in enumerate(lines, 1):
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                self.fail("event log line {} is not one JSON object (interleaved writes?): {!r}".format(
                    number, line[:200]))
        events.sort(key=lambda item: item["t"])
        library = self.library
        runs_by_pid = {run.pid: run for run in self.runs}

        def label(pid):
            run = runs_by_pid.get(pid)
            return run.label if run else "pid {}".format(pid)

        def stamp(ns):
            return "{:.3f} s".format((ns - self.runs[0].started_ns) / 1e9) if self.runs else str(ns)

        # Exit codes and tracebacks.
        takeover_lines = []
        skip_lines = 0
        for run in self.runs:
            text = run.output()
            if "Traceback" in text:
                self.fail("{} printed a traceback; log '{}':\n{}".format(run.label, run.log_path,
                                                                        _around(text, "Traceback")))
            expected = (-signal.SIGKILL,) if run.killed else (0,)
            if run.code not in expected:
                self.fail("{} ended with exit code {} (expected {}); last lines of '{}':\n{}".format(
                    run.label, run.code, " or ".join(str(code) for code in expected), run.log_path,
                    _tail(text, 12)))
            for line in text.splitlines():
                if "taking over" in line and "lock" in line:
                    takeover_lines.append((run, line.strip()))
                if line.lstrip().startswith("skipping folder '"):
                    skip_lines += 1
        self.counts["takeovers"] = len(takeover_lines)
        self.counts["skipped-folder lines"] = skip_lines

        # Lock holds per host run and folder: [acquired, released].
        holds = []
        open_holds = {}
        for event in events:
            kind = event.get("kind")
            if kind == "lock-acquired":
                key = (event["pid"], event["folder"])
                if key in open_holds:
                    self.fail("{} acquired the lock of '{}' twice without a release".format(
                        label(event["pid"]), short(event["folder"], library)))
                open_holds[key] = event["t"]
            elif kind == "lock-release":
                key = (event["pid"], event["folder"])
                if key not in open_holds:
                    self.fail("{} released the lock of '{}' it had not acquired".format(
                        label(event["pid"]), short(event["folder"], library)))
                    continue
                holds.append({"pid": event["pid"], "folder": event["folder"], "start": open_holds.pop(key),
                              "end": event["t"], "open": False})
        for (pid, folder), start in open_holds.items():
            run = runs_by_pid.get(pid)
            if run is not None and run.killed:
                end = run.stopped_ns
            else:
                self.fail("{} never released the lock of '{}'".format(label(pid), short(folder, library)))
                end = run.ended_ns if run is not None and run.ended_ns else start
            holds.append({"pid": pid, "folder": folder, "start": start, "end": end, "open": True})

        victim = self.victim
        cut = victim.stopped_ns if victim is not None else None

        def victim_hold(hold):
            """The victim's hold of its folder that was in force when it was stopped or killed."""
            return (victim is not None and hold["pid"] == victim.pid and hold["folder"] == self.victim_folder
                    and hold["start"] <= cut <= hold["end"])

        for index, first in enumerate(holds):
            for second in holds[index + 1:]:
                if first["folder"] != second["folder"] or first["pid"] == second["pid"]:
                    continue
                if first["start"] < second["end"] and second["start"] < first["end"]:
                    earlier, later = sorted((first, second), key=lambda hold: hold["start"])
                    if victim_hold(earlier) and later["start"] > cut:
                        continue  # taken over from the stopped or killed victim: its writes are checked below
                    self.fail("exclusion: {} and {} held the lock of '{}' at the same time ({} to {} and {} to {})".format(
                        label(earlier["pid"]), label(later["pid"]), short(first["folder"], library),
                        stamp(earlier["start"]), stamp(earlier["end"]), stamp(later["start"]), stamp(later["end"])))

        # Every write inside a lock its host held (lock files themselves excepted); the victim writes nothing
        # in its folder after it was stopped, except the removal of the part file of its own attempt (a name
        # no other run uses), which it removes once it finds the lock lost.
        writes = [event for event in events if event.get("kind") == "fs"]
        victim_part = None
        if victim is not None:
            victim_part = next((event["output"] for event in events if event.get("kind") == "ffmpeg-start"
                                and event["pid"] == self.victim_encode), None)
        outside = []
        own_removals = 0
        for event in writes:
            for path in event["paths"]:
                if is_lock_machinery(path):
                    continue
                folder = folder_of_path(path)
                inside = any(hold["pid"] == event["pid"] and hold["folder"] == folder
                             and hold["start"] <= event["t"] <= hold["end"] for hold in holds)
                late = (victim is not None and event["pid"] == victim.pid and folder == self.victim_folder
                        and event["t"] > cut)
                if late and inside and event["op"] == "os.remove" and path == victim_part:
                    own_removals += 1
                    continue
                if not inside or late:
                    outside.append("{} {} '{}' at {}{}".format(
                        label(event["pid"]), event["op"], short(path, library), stamp(event["t"]),
                        " (after it was stopped)" if late else " (no lock of '{}' held)".format(short(folder, library))))
        if outside:
            self.fail("exclusion: {} write(s) outside a lock the writer held:\n      {}".format(
                len(outside), "\n      ".join(outside[:20])))
        self.counts["writes"] = len(writes)
        if own_removals:
            self.notes.append("the stopped {} removed the part file of its own attempt '{}' after the SIGCONT".format(
                victim.label, short(victim_part, library)))

        # Encodes: [start, exit] per wrapper pid.
        encodes = {}
        existed = []
        for event in events:
            if event.get("kind") == "ffmpeg-start":
                encodes[event["pid"]] = {"start": event, "exit": None}
            if event.get("kind") in ("ffmpeg-start", "ffmpeg-open") and event.get("created") != "later":
                if event.get("created") == "existed":
                    existed.append(event)
                elif event.get("created") != "created":
                    self.fail("{} started an encode into '{}' whose part file could not be made: {}".format(
                        label(event["ppid"]), short(event["output"], library), event.get("created")))
            elif event.get("kind") == "ffmpeg-exit":
                if event["pid"] in encodes:
                    encodes[event["pid"]]["exit"] = event
        intervals = []
        interrupted = 0
        for pid, item in encodes.items():
            start, end_event = item["start"], item["exit"]
            run = runs_by_pid.get(start["ppid"])
            if end_event is not None:
                end = end_event["t"]
            elif run is not None and run.killed:
                end = run.gone_ns or run.stopped_ns
                interrupted += 1
            else:
                self.fail("the encode of '{}' by {} has no exit line".format(short(start["output"], library),
                                                                            label(start["ppid"])))
                end = start["t"]
            intervals.append({"pid": pid, "run": start["ppid"], "folder": os.path.dirname(start["output"]),
                              "output": start["output"], "start": start["t"], "end": end, "exit": end_event})
        self.counts["encodes"] = len(intervals)
        for event in existed:
            # The part path existed when this encode's wrapper made it: name the removals before it and every
            # other encode of the same path that was running at that moment.
            path = event["output"]
            removals = [item for item in writes if item["op"] == "os.remove" and path in item["paths"]
                        and item["t"] <= event["t"]]
            running = [interval for interval in intervals if interval["output"] == path
                       and interval["pid"] != event["pid"] and interval["start"] <= event["t"] <= interval["end"]]
            self.fail("two writers: when {}'s encoder started at {}, its part file '{}' already existed{}{}".format(
                label(event["ppid"]), stamp(event["t"]), short(path, library),
                "; it was last removed by {} at {}".format(label(removals[-1]["pid"]), stamp(removals[-1]["t"]))
                if removals else "",
                "; still running then: {} (an encoder that opens the part path after the removal creates it "
                "again, and both encoders then write the one file)".format(_encodes_text(running, label, stamp))
                if running else "; no other logged encode of it was running"))
        self.counts["interrupted encodes"] = interrupted
        # Two writers of one file under any names: each wrapper holds its part file open from its open to its
        # exit, so two encodes whose held inodes are equal while both are held wrote the same file.
        held = {}
        for event in events:
            if event.get("kind") in ("ffmpeg-start", "ffmpeg-open") and event.get("inode") is not None:
                held[event["pid"]] = (event["inode"], event["t"])
        for index, first in enumerate(intervals):
            for second in intervals[index + 1:]:
                if first["pid"] not in held or second["pid"] not in held:
                    continue
                (first_inode, first_from), (second_inode, second_from) = held[first["pid"]], held[second["pid"]]
                if first_inode == second_inode and first_from < second["end"] and second_from < first["end"]:
                    self.fail("two writers: {}'s encode into '{}' and {}'s into '{}' held one file (inode {}) at "
                              "the same time".format(label(first["run"]), short(first["output"], library),
                                                     label(second["run"]), short(second["output"], library),
                                                     first_inode))

        def victim_encode(interval):
            return victim is not None and interval["pid"] == self.victim_encode

        for index, first in enumerate(intervals):
            for second in intervals[index + 1:]:
                if not (first["start"] < second["end"] and second["start"] < first["end"]):
                    continue
                if first["run"] == second["run"]:
                    if options.jobs == 1:
                        self.fail("{} ran two encodes at once with --jobs 1: '{}' and '{}'".format(
                            label(first["run"]), short(first["output"], library), short(second["output"], library)))
                    continue
                if first["folder"] != second["folder"]:
                    continue
                earlier, later = sorted((first, second), key=lambda interval: interval["start"])
                if victim_encode(earlier) and self.scenario == "freeze" and later["start"] > cut:
                    continue
                self.fail("exclusion: {} and {} encoded in '{}' at the same time: '{}' ({} to {}) and '{}' ({} to "
                          "{})".format(label(earlier["run"]), label(later["run"]), short(first["folder"], library),
                                       os.path.basename(earlier["output"]), stamp(earlier["start"]),
                                       stamp(earlier["end"]), os.path.basename(later["output"]),
                                       stamp(later["start"]), stamp(later["end"])))

        # Parallel work: two hosts encoding in different folders at one moment.
        parallel = any(first["run"] != second["run"] and first["folder"] != second["folder"]
                       and first["start"] < second["end"] and second["start"] < first["end"]
                       for index, first in enumerate(intervals) for second in intervals[index + 1:])
        self.counts["parallel"] = parallel
        if self.scenario == "plain" and len(self.hosts) > 1 and not parallel:
            self.fail("no two hosts ever encoded in different folders at the same moment, so this run proves "
                      "nothing about parallel work (raise --delay)")

        # Once only, and the final state of every record planned at the start.
        records = read_records(library)
        accepted_count = 0
        discarded = 0
        for source, before in sorted(self.planned.items()):
            name = short(source, library)
            after = records.get(source)
            if not isinstance(after, dict):
                self.fail("the record of '{}' is gone or unreadable".format(name))
                continue
            if after.get("status") != "verified":
                self.fail("'{}' ends with status '{}' (expected 'verified'); convert stage: {}".format(
                    name, after.get("status"), _brief((after.get("stages") or {}).get("convert"))))
                continue
            convert = (after.get("stages") or {}).get("convert") or {}
            output_name = (after.get("output") or {}).get("name")
            output = os.path.join(os.path.dirname(source), output_name or "")
            if not output_name or not os.path.isfile(output):
                self.fail("the output '{}' of '{}' does not exist".format(output_name, name))
                continue
            part_name = convert.get("part_path") or ".{}.{}.part".format(output_name, (after.get("id") or "")[:8])
            part = os.path.join(os.path.dirname(source), part_name)
            ours = [interval for interval in intervals if record_part_key(interval["output"]) == record_part_key(part)]
            status = os.stat(output)
            digest = sha256_of(output)
            recorded = (after.get("output") or {}).get("sha256")
            if recorded and recorded != digest:
                self.fail("the output of '{}' has SHA-256 {} but its record says {}".format(name, digest[:16],
                                                                                         recorded[:16]))
            writer = self.soak.host_of_ffmpeg_path((convert.get("ffmpeg") or {}).get("path"))
            accepted = [interval for interval in ours if interval["exit"] is not None
                        and interval["exit"].get("status") == 0 and interval["exit"].get("inode") == status.st_ino]
            if len(accepted) != 1:
                self.fail("once only: the output of '{}' (inode {}) is the file of {} logged encode(s), not one{}; "
                          "encodes of its part file: {}".format(
                              name, status.st_ino, len(accepted),
                              " (no wrapper made it: an encoder created it by opening the part path itself)"
                              if not accepted else "", _encodes_text(ours, label, stamp)))
                continue
            accepted = accepted[0]
            accepted_count += 1
            if accepted["exit"].get("sha256") != digest:
                self.fail("the output of '{}' differs from what the accepted encode by {} wrote ({} != {})".format(
                    name, label(accepted["run"]), digest[:16], str(accepted["exit"].get("sha256"))[:16]))
            encoder = runs_by_pid.get(accepted["run"])
            if encoder is None or writer != encoder.host:
                self.fail("the record of '{}' names ffmpeg '{}' (host {}) but the encode in place was {}'s".format(
                    name, (convert.get("ffmpeg") or {}).get("path"), writer, label(accepted["run"])))
            for other in ours:
                if other is accepted:
                    continue
                if other["exit"] is None and runs_by_pid.get(other["run"]) is not None \
                        and runs_by_pid[other["run"]].killed:
                    self.notes.append("'{}': the killed {}'s encode was cut off".format(name, label(other["run"])))
                    continue
                if victim_encode(other) and self.scenario == "freeze":
                    discarded += 1
                    self.notes.append("'{}': the frozen {}'s encode ended at {} with status {} after the SIGCONT "
                                      "({}); its result (inode {}) was discarded, the output is {}'s (inode {})".format(
                                          name, label(other["run"]), stamp(other["end"]),
                                          (other["exit"] or {}).get("status"),
                                          "after" if other["end"] > (victim.continued_ns or 0) else "before",
                                          (other["exit"] or {}).get("inode"), label(accepted["run"]), status.st_ino))
                    continue
                self.fail("once only: '{}' was encoded again: {}".format(name, _encodes_text([other], label, stamp)))
        self.counts["files"] = len(self.planned)
        self.counts["accepted encodes"] = accepted_count
        self.counts["discarded encodes"] = discarded
        extra = [source for source in records if source not in self.planned]
        if extra:
            self.fail("records appeared that were not planned at the start: {}".format(
                [short(source, library) for source in extra]))

        # The victim's own story.
        if victim is not None:
            folder = short(self.victim_folder, library)
            taken = [line for run, line in takeover_lines
                     if self.victim_folder in line and self.pose(victim.host) in line]
            if not taken:
                self.fail("no host printed a takeover of '{}' from {}".format(folder, self.pose(victim.host)))
            stray = [line for run, line in takeover_lines if self.victim_folder + os.sep + RECORD_DIR not in line]
            if stray:
                self.fail("takeover of a folder other than the victim's '{}' (a live lock judged stale?):\n      "
                          "{}".format(folder, "\n      ".join(stray)))
            if self.scenario == "freeze":
                text = victim.output()
                if "lost the lock of folder '{}'".format(self.victim_folder) not in text:
                    self.fail("the frozen {} never said it lost the lock of '{}'; last lines:\n{}".format(
                        victim.label, folder, _tail(text, 12)))
                frozen = [interval for interval in intervals if victim_encode(interval)]
                if frozen and frozen[0]["exit"] is not None:
                    self.notes.append("the frozen encode exited {} after the SIGCONT with status {}".format(
                        "{:.3f} s".format((frozen[0]["end"] - victim.continued_ns) / 1e9)
                        if victim.continued_ns else "?", frozen[0]["exit"].get("status")))
        elif takeover_lines:
            self.fail("{} takeover(s) in a scenario where every host stayed alive (a live lock judged stale?):\n"
                      "      {}".format(len(takeover_lines), "\n      ".join(line for _run, line in takeover_lines)))

        # Leftovers once every host has exited.
        leftovers = []
        kept = 0
        for folder in walk_folders(library):
            for name in os.listdir(folder):
                if name.endswith(".part"):
                    leftovers.append(short(os.path.join(folder, name), library))
            directory = os.path.join(folder, RECORD_DIR)
            if os.path.isdir(directory):
                for name in os.listdir(directory):
                    if name in LEFTOVER_NAMES or name.startswith(LEFTOVER_PREFIXES) or name.endswith(".tmp"):
                        leftovers.append(short(os.path.join(directory, name), library))
                    elif name == "lock.takeover":
                        kept += 1
        if leftovers:
            self.fail("left behind after every host exited: {}".format(", ".join(sorted(leftovers))))
        self.counts["lock.takeover files"] = kept

        if self.verify_run is not None:
            text = self.verify_run.output()
            self.counts["verify"] = self.verify_run.code
            if self.verify_run.code != 0:
                self.fail("the final verify ended with {}:\n{}".format(self.verify_run.code, _tail(text, 15)))
        if self.scenario == "staggered" and self.rerun is not None and not self.rerun_overlapped:
            self.fail("the re-run of {} did not start while another host still worked".format(self.rerun.host))
        for run in self.runs:
            if group_alive(run.pid):
                self.fail("the process group of {} (pid {}) still has a process after the scenario".format(
                    run.label, run.pid))

    def summary(self):
        verdict = "FAIL" if self.failures else "PASS"
        counts = self.counts
        return ("{} {:<9} {} hosts round {}: {} files, {} runs, {} encodes ({} interrupted, {} discarded), "
                "{} takeover(s), {} skipped-folder line(s), parallel: {}, {:.1f} s".format(
                    verdict, self.scenario, len(self.hosts), self.tag.rsplit("round", 1)[-1],
                    counts.get("files", "?"), len(self.runs), counts.get("encodes", "?"),
                    counts.get("interrupted encodes", "?"), counts.get("discarded encodes", "?"),
                    counts.get("takeovers", "?"), counts.get("skipped-folder lines", "?"),
                    "yes" if counts.get("parallel") else "no", self.elapsed))


def _tail(text, count):
    return "\n".join("      | " + line for line in text.rstrip().splitlines()[-count:])


def _around(text, word):
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if word in line:
            return "\n".join("      | " + item for item in lines[index:index + 25])
    return ""


def _brief(stage):
    if not isinstance(stage, dict):
        return repr(stage)
    return {key: stage.get(key) for key in ("outcome", "skipped", "error") if key in stage}


def _encodes_text(intervals, label, stamp):
    if not intervals:
        return "none"
    return "; ".join("{} {} to {} status {} inode {}".format(
        label(item["run"]), stamp(item["start"]), stamp(item["end"]),
        (item["exit"] or {}).get("status", "none"), (item["exit"] or {}).get("inode")) for item in intervals)


# ---------------------------------------------------------------- command line

def parse_arguments(argv):
    if "--as-host" in argv:
        split = argv.index("--") if "--" in argv else len(argv)
        own, vidfunnel_args = argv[:split], argv[split + 1:]
        parser = argparse.ArgumentParser(prog="soak_hosts.py --as-host")
        parser.add_argument("--as-host", required=True, metavar="NAME")
        parser.add_argument("--events", required=True)
        parser.add_argument("--watch", required=True)
        parser.add_argument("--beat", type=float, default=1.0)
        parser.add_argument("--stale", type=float, default=8.0)
        parser.add_argument("--marker-stale", type=float, default=4.0)
        parser.add_argument("--set", action="append", metavar="NAME=SECONDS")
        parser.add_argument("--sabotage", action="store_true")
        return parser.parse_args(own), vidfunnel_args
    parser = argparse.ArgumentParser(
        prog="soak_hosts.py",
        description="Simulated machines convert one small library at once, with kills and freezes; the checks "
                    "prove one host per folder and one encode per file (HANDOFF.md section 9).")
    parser.add_argument("--version", action="version", version="soak_hosts.py " + VERSION)
    parser.add_argument("--hosts", default="2,3,4", help="host counts to run, comma-separated (default 2,3,4)")
    parser.add_argument("--scenarios", default=",".join(SCENARIOS),
                        help="scenarios to run, comma-separated, of {} (default all)".format(", ".join(SCENARIOS)))
    parser.add_argument("--rounds", type=int, default=1, help="repeat each scenario this often (default 1)")
    parser.add_argument("--library", metavar="PATH",
                        help="build the libraries in this folder, which must be empty or one this script made "
                             "(default: a folder in the work folder)")
    parser.add_argument("--work", metavar="DIR", help="work folder for logs and wrappers (default: a new temporary "
                        "folder)")
    parser.add_argument("--keep", action="store_true", help="keep the libraries and logs after a full pass")
    parser.add_argument("--folders", type=int, default=6, help="folders in the library (default 6)")
    parser.add_argument("--loose", type=int, default=5, help="clips in the library root (default 5)")
    parser.add_argument("--jobs", type=int, default=1, help="convert --jobs of each host (default 1)")
    parser.add_argument("--delay", type=float, default=1.5,
                        help="seconds each encode is held open after ffmpeg ends, so runs overlap (default 1.5)")
    parser.add_argument("--beat", type=float, default=1.0, help="heartbeat interval of the hosts (default 1 s)")
    parser.add_argument("--stale", type=float, default=8.0,
                        help="silence after which a host's lock may be taken over (default 8 s)")
    parser.add_argument("--marker-stale", type=float, default=4.0,
                        help="value for a TAKEOVER*_SECONDS constant when the tree has one (default 4 s)")
    parser.add_argument("--set", action="append", metavar="NAME=SECONDS",
                        help="set another vidfunnel.record constant in every host")
    parser.add_argument("--stagger", type=float, default=2.0, help="seconds between host starts in 'staggered'")
    parser.add_argument("--deadline", type=float, default=120.0,
                        help="seconds any one wait may take before the scenario fails (default 120)")
    parser.add_argument("--plan-each", action="store_true",
                        help="plan every scenario's library afresh instead of copying one planned template")
    parser.add_argument("--ffmpeg", dest="real_ffmpeg", default="ffmpeg", help="the real ffmpeg (default: PATH)")
    parser.add_argument("--hold-before-open", action="store_true",
                        help="hold each encode for --delay s before its part file is made and ffmpeg starts, "
                             "instead of after ffmpeg ends (a probe of that window; not part of the default run)")
    parser.add_argument("--resume-after-cleanup", action="store_true",
                        help="in 'freeze', send SIGCONT only once the host that took the folder over has started "
                             "its own encode of the frozen host's file (after its part-file cleanup)")
    parser.add_argument("--sabotage-locks", action="store_true", help=argparse.SUPPRESS)
    options = parser.parse_args(argv)
    try:
        options.host_counts = [int(item) for item in options.hosts.split(",") if item.strip()]
    except ValueError:
        parser.error("--hosts takes numbers such as 2,3,4")
    if not options.host_counts or min(options.host_counts) < 2 or max(options.host_counts) > len(HOST_NAMES):
        parser.error("--hosts takes counts from 2 to {}".format(len(HOST_NAMES)))
    options.scenario_list = [item.strip() for item in options.scenarios.split(",") if item.strip()]
    unknown = [item for item in options.scenario_list if item not in SCENARIOS]
    if unknown or not options.scenario_list:
        parser.error("unknown scenario(s) {}; choose from {}".format(unknown, ", ".join(SCENARIOS)))
    if options.rounds < 1 or options.jobs < 1 or options.folders < 1 or options.loose < 0:
        parser.error("--rounds, --jobs and --folders take 1 or more, --loose 0 or more")
    return options, None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    options, vidfunnel_args = parse_arguments(argv)
    if vidfunnel_args is not None:
        return host_main(options, vidfunnel_args)
    soak = Soak(options)

    def stop(signum, frame):
        raise KeyboardInterrupt()

    for name in ("SIGTERM", "SIGHUP"):
        signal.signal(getattr(signal, name), stop)
    say("soak_hosts.py {} in '{}'; work folder '{}', libraries in '{}'".format(VERSION, REPO_ROOT, soak.work,
                                                                              soak.area))
    say("hosts beat every {} s, a silent lock is stale after {} s, encodes are held {} s, convert --jobs {}".format(
        options.beat, options.stale, options.delay, options.jobs))
    load_before = os.getloadavg()
    say("load average at the start: {:.2f} {:.2f} {:.2f} on {} CPUs".format(*(load_before + (os.cpu_count(),))))
    started = time.monotonic()
    scenes = []
    interrupted = False
    try:
        soak.prepare_area()
        soak.plan_template()
        for round_number in range(1, options.rounds + 1):
            for count in options.host_counts:
                for scenario in options.scenario_list:
                    scene = soak.run_scenario(scenario, count, round_number)
                    scenes.append(scene)
                    say(scene.summary())
                    for note in scene.notes:
                        say("    note: " + note)
                    for failure in scene.failures:
                        say("    FAIL: " + failure)
                    if scene.failures:
                        say("    logs: '{}', library: '{}'".format(scene.logs, scene.library))
    except KeyboardInterrupt:
        interrupted = True
        say("soak_hosts: interrupted")
    finally:
        soak.cleanup()
    elapsed = time.monotonic() - started
    failed = [scene for scene in scenes if scene.failures]
    load_after = os.getloadavg()
    say("")
    say("== summary ==")
    for scene in scenes:
        say(scene.summary())
    say("load average at the end: {:.2f} {:.2f} {:.2f}".format(*load_after))
    say("{} scenario run(s), {} passed, {} failed, in {:.0f} min {:.0f} s".format(
        len(scenes), len(scenes) - len(failed), len(failed), elapsed // 60, elapsed % 60))
    if failed or interrupted or options.keep:
        say("work folder kept: '{}'".format(soak.work))
        if options.library:
            say("libraries kept: '{}'".format(soak.area))
    else:
        shutil.rmtree(soak.work, ignore_errors=True)
        if options.library:
            for name in ("template", "runs"):
                shutil.rmtree(os.path.join(soak.area, name), ignore_errors=True)
    if interrupted:
        return 130
    return 1 if failed or not scenes else 0


if __name__ == "__main__":
    sys.exit(main())

# End of File #
