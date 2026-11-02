"""convert: run the planned ffmpeg commands to a hidden temp file, rename into place, write PAR2, verify.

HANDOFF.md sections 2.4, 2.5, 5 (the size rule), 6.3, 6.7 and 6.10.
`convert` runs exactly the argv that `plan` stored under stages.plan.argv,
with its last element (the output path) replaced by a hidden '.NAME.part'
file in the same folder. On success the part file is renamed into place
(the interim '<stem>.vidfunnel.mp4' name when the source is itself an MP4),
the output gets the source's mtime, the subtitle sidecars are written the
same way, the size rule is applied, PAR2 is written when asked, and verify
runs on the result when vidfunnel.verify is present.

What convert writes into the record:

- stages.convert, on success: outcome 'ok', output_path (file name),
  part_path (file name), elapsed_s, ffmpeg_stderr (last 4000 characters),
  sidecars_written (file names), sidecar_errors (only when one failed),
  par2_files (file names), par2_error (only when par2 failed), output_size,
  source_size, size_ratio (None when the source size is unknown);
- stages.convert on an ffmpeg failure: outcome 'failed', error,
  returncode, stderr (last 4000 characters), argv; status 'failed';
- stages.convert when the output came out larger than the source and
  --keep-larger was not given: outcome 'larger', output_size, source_size;
  the output is removed, flag 'output-larger', status 'failed';
- stages.convert when the output name is taken by a file that is not
  ours: outcome 'output-collision', error; flag 'output-collision', status
  'failed';
- output.size and output.fingerprint (the cheap fingerprint after the
  mtime is set, so a later run can skip the file without verify having
  run), status 'converted' (or 'verified' once verify has run);
- flags 'keep-larger', 'sidecar-failed', 'par2-skipped', 'par2-failed',
  'source-changed' (status 'source-changed' too) as they apply.

The record is saved after every change of state, so an interrupted run
leaves a record that says what happened. A record that is not 'planned' is
refused without being touched, except that a converted or verified record
whose output still matches its fingerprint is reported as already converted.
"""

VERSION = "20261102"

import concurrent.futures
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

from vidfunnel.ffmpeg import FfmpegError, ToolNotFound
from vidfunnel.ffmpeg import run as run_tool
from vidfunnel.progress import Progress, StageSummary, ffmpeg_progress_callback, say
from vidfunnel.record import (
    RECORD_DIR,
    FolderLock,
    LockHeld,
    RecordError,
    add_flag,
    add_stage,
    cheap_fingerprint,
    fingerprint_matches,
    iter_records,
    load_record,
    record_path,
    save_record,
    set_status,
)

PART_SUFFIX = ".part"
STDERR_TAIL = 4000
OUTCOMES = ("converted", "verified", "skipped", "failed", "larger")
ACTION_WORDS = {"remux": "remux", "reencode": "re-encode"}
DONE_STATUSES = ("converted", "verified")


# --- names ------------------------------------------------------------------------------


def part_path(output_path):
    """The temp file convert writes to: same directory, '.<name>.part' (hidden from Jellyfin and Finder)."""
    output_path = Path(output_path)
    return output_path.parent / ("." + output_path.name + PART_SUFFIX)


def output_path_for(record, folder):
    """folder / (output.interim_name or output.name)."""
    output = record.get("output") or {}
    name = output.get("interim_name") or output.get("name")
    if not name:
        raise ValueError("record {} has no output name".format(record.get("id")))
    return Path(folder) / name


def record_key(folder, record):
    """The path record.py keys this record by: the source file, the first VOB, or '<dvd dir>_titleNN'."""
    source = record.get("source") or {}
    name = source.get("name")
    if source.get("kind") == "dvd-title":
        return Path(folder) / "{}_title{:02d}".format(name, int(source.get("dvd_title") or 0))
    return Path(folder) / name


def _label(record):
    source = record.get("source") or {}
    if source.get("kind") == "dvd-title":
        return "{} title {}".format(source.get("name"), source.get("dvd_title"))
    if source.get("kind") == "vob-group":
        count = len(source.get("members") or [])
        return "{} ({} VOB{})".format(source.get("name"), count, "" if count == 1 else "s")
    return source.get("name") or "?"


def expected_duration(record):
    """The source duration in seconds from stages.probe.format.duration, or None (progress then shows elapsed)."""
    probe = (record.get("stages") or {}).get("probe") or {}
    duration = (probe.get("format") or {}).get("duration")
    try:
        value = float(duration)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _tail(text):
    text = text or ""
    return text[-STDERR_TAIL:]


# --- checks -----------------------------------------------------------------------------


def _source_files(folder, record):
    """[(path, recorded fingerprint)] to check before converting, first entry giving the mtime to copy."""
    source = record.get("source") or {}
    kind = source.get("kind") or "file"
    folder = Path(folder)
    if kind == "vob-group":
        return [(folder / member.get("name"), member.get("fingerprint")) for member in source.get("members") or []]
    if kind == "dvd-title":
        return [(folder / source.get("name") / (source.get("fingerprint_of") or "VIDEO_TS.IFO"),
                 source.get("fingerprint"))]
    return [(folder / source.get("name"), source.get("fingerprint"))]


def source_change(folder, record):
    """None when every source file still matches its recorded fingerprint, else a reason naming the file."""
    files = _source_files(folder, record)
    if not files:
        return "the record lists no source files"
    for path, recorded in files:
        try:
            current = cheap_fingerprint(path)
        except OSError as error:
            return "'{}' cannot be read: {}".format(path.name, error)
        if not fingerprint_matches(recorded, current):
            return "'{}' no longer matches the fingerprint taken at plan".format(path.name)
    return None


def skip_reason(record, output_path):
    """'already converted' when the status is converted/verified and the output matches its fingerprint; else None."""
    if record.get("status") not in DONE_STATUSES:
        return None
    recorded = (record.get("output") or {}).get("fingerprint")
    if not recorded:
        return None
    try:
        current = cheap_fingerprint(output_path)
    except OSError:
        return None
    if fingerprint_matches(recorded, current):
        return "already converted"
    return None


def _refusal(record, folder, output_path):
    """Why a record cannot be converted as it stands, or None. Touches nothing."""
    status = record.get("status")
    if status != "planned":
        if status in DONE_STATUSES:
            return ("status is '{}' but the output '{}' is missing or changed since; "
                    "plan again with --replan to convert again".format(status, output_path.name))
        return "status is '{}', not 'planned'".format(status)
    if record.get("action") not in ACTION_WORDS:
        return "action is '{}', not remux or reencode".format(record.get("action"))
    plan = (record.get("stages") or {}).get("plan") or {}
    if "convert" in (record.get("stages") or {}):
        return "the record already has a convert stage; plan again with --replan"
    argv = plan.get("argv")
    if not isinstance(argv, list) or len(argv) < 4 or argv[-3:-1] != ["-f", "mp4"]:
        return "stages.plan.argv does not end in '-f mp4 <output>'"
    if os.path.abspath(argv[-1]) != os.path.abspath(str(output_path)):
        return ("the planned command writes '{}', not '{}'; the folder may have moved since plan: "
                "plan again with --replan".format(argv[-1], output_path))
    if not (os.path.isfile(argv[0]) and os.access(argv[0], os.X_OK)):
        return "ffmpeg '{}' named in the plan does not exist here; plan again on this machine".format(argv[0])
    return None


def _ours(tools, path, record):
    """True when the file at path carries our identity tag with this source's 'src'."""
    from vidfunnel.plan import read_identity
    sha = (record.get("source") or {}).get("sha256") or ""
    tag = read_identity(tools, path) if tools is not None else None
    return bool(tag and sha and tag.get("src") == sha[:16])


# --- running ----------------------------------------------------------------------------


def _remove(path):
    try:
        os.unlink(str(path))
        return True
    except FileNotFoundError:
        return False


def _stream_of(progress):
    if progress is None:
        return None, False, False
    return (getattr(progress, "stream", None), bool(getattr(progress, "quiet", False)),
            bool(getattr(progress, "verbose", False)))


def write_par2(output_path, redundancy, progress=None):
    """par2create -r N -n 1 -q next to the output; returns the PAR2 files written. Raises OSError on failure.

    Stale PAR2 files of the same name are removed first. An elapsed-time line
    runs while par2 works, since it reports no measurable progress.
    """
    stream, quiet, _verbose = _stream_of(progress)
    output_path = Path(output_path)
    par2 = shutil.which("par2")
    if par2 is None:
        raise OSError("'par2' is not on PATH")
    for stale in _par2_files(output_path):
        _remove(stale)
    index_name = output_path.name + ".par2"
    argv = [par2, "create", "-r{}".format(int(redundancy)), "-n1", "-q", "--", index_name, output_path.name]
    say("writing PAR2 ({} %) for '{}'".format(int(redundancy), output_path.name), stream)
    process = subprocess.Popen(argv, cwd=str(output_path.parent), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                               encoding="utf-8", errors="replace")
    collected = []
    reader = threading.Thread(target=lambda: collected.append(process.stdout.read()), daemon=True)
    reader.start()
    with Progress("par2", total=None, unit=None, stream=stream, quiet=quiet) as live:
        try:
            while True:
                try:
                    process.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    live.update()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            reader.join()
            process.stdout.close()
    if process.returncode != 0:
        for partial in _par2_files(output_path):
            _remove(partial)
        raise OSError("par2 exited with status {} for '{}': {}".format(
            process.returncode, output_path.name, "".join(collected).strip()[-STDERR_TAIL:]))
    files = _par2_files(output_path)
    if not files:
        raise OSError("par2 reported success but wrote no '{}' files".format(index_name))
    return files


def _par2_files(output_path):
    output_path = Path(output_path)
    found = []
    try:
        names = sorted(os.listdir(str(output_path.parent)))
    except OSError:
        return found
    prefix = output_path.name + "."
    for name in names:
        if name == output_path.name + ".par2" or (name.startswith(prefix + "vol") and name.endswith(".par2")):
            found.append(output_path.parent / name)
    return found


def _write_sidecars(plan, stream, verbose):
    """Run each sidecar argv to its own part file and rename; (names written, [error dicts])."""
    written = []
    errors = []
    for entry in plan.get("sidecars") or []:
        final = Path(entry.get("path") or "")
        argv = list(entry.get("argv") or [])
        if not argv or os.path.abspath(argv[-1]) != os.path.abspath(str(final)):
            errors.append({"path": final.name, "error": "sidecar argv does not end in its path"})
            continue
        if final.exists():
            errors.append({"path": final.name, "error": "'{}' already exists; not overwritten".format(final.name)})
            continue
        temp = part_path(final)
        argv[-1] = str(temp)
        say("  writing sidecar '{}' ({})".format(final.name, entry.get("kind")), stream)
        if verbose:
            say("  " + shlex.join(argv), stream)
        _remove(temp)
        try:
            run_tool(argv)
            os.replace(str(temp), str(final))
            written.append(final.name)
        except (FfmpegError, ToolNotFound, OSError) as error:
            errors.append({"path": final.name, "error": str(error),
                           "stderr": _tail(getattr(error, "stderr", None))})
            say("  sidecar '{}' failed: {}".format(final.name, error), stream)
        finally:
            _remove(temp)
    return written, errors


def _verify(tools, key, record, options, progress, stream):
    """Run vidfunnel.verify.verify_record when the module exists; the record it returns, saved."""
    try:
        from vidfunnel.verify import verify_record
    except ModuleNotFoundError as error:
        if error.name != "vidfunnel.verify":
            raise
        say("  verify is not available in this build; the record stays at 'converted' "
            "(run 'vidfunnel verify' later)", stream)
        return record
    try:
        verified = verify_record(tools, key, record, options, progress)
    except (FfmpegError, ToolNotFound, OSError, RecordError, ValueError) as error:
        say("  verify failed for '{}': {}; the record stays at 'converted'".format(_label(record), error), stream)
        return record
    if verified is not None and verified is not record:
        record.clear()
        record.update(verified)
    save_record(key, record)
    return record


def convert_one(tools, source, record, options, progress=None, position=None, total=None):
    """Convert one planned record; returns the convert stage dict (or {'outcome': 'skipped', 'skipped': reason}).

    `source` is the record's key path (the source file, the first VOB of a
    group, or '<dvd folder>_titleNN' beside the DVD folder); its parent is
    the folder. `options` carries par2 (override or None), keep_larger,
    verbose. `progress` is None or carries stream and quiet. The record is
    changed in place and saved after each change of state.
    """
    stream, quiet, verbose = _stream_of(progress)
    verbose = verbose or bool(getattr(options, "verbose", False))
    key = Path(os.path.abspath(os.fspath(source)))
    folder = key.parent
    output_path = output_path_for(record, folder)
    label = _label(record)

    reason = skip_reason(record, output_path)
    if reason is None:
        reason = _refusal(record, folder, output_path)
    if reason is not None:
        say("skipping '{}': {}".format(label, reason), stream)
        return {"outcome": "skipped", "skipped": reason}

    changed = source_change(folder, record)
    if changed is not None:
        add_flag(record, "source-changed")
        set_status(record, "source-changed")
        save_record(key, record)
        reason = "source changed since plan: {}; plan again".format(changed)
        say("refusing '{}': {}".format(label, reason), stream)
        return {"outcome": "skipped", "skipped": reason}

    if output_path.exists() and not _ours(tools, output_path, record):
        error = "'{}' exists and is not this record's output (no matching vidfunnel tag); not overwritten".format(
            output_path.name)
        stage = {"outcome": "output-collision", "error": error}
        add_stage(record, "convert", stage)
        add_flag(record, "output-collision")
        set_status(record, "failed")
        save_record(key, record)
        say("refusing '{}': {}".format(label, error), stream)
        return record["stages"]["convert"]

    plan = record["stages"]["plan"]
    action_word = ACTION_WORDS[record["action"]]
    temp = part_path(output_path)
    argv = list(plan["argv"])
    argv[-1] = str(temp)
    counter = "[convert {}/{}]".format(position, total) if position is not None and total else "[convert]"
    say("{} {} '{}' -> '{}'".format(counter, action_word, label, output_path.name), stream)
    if verbose:
        say(shlex.join(argv), stream)

    if _remove(temp):
        say("  removed a stale '{}' from an earlier run".format(temp.name), stream)
    started = time.monotonic()
    duration = expected_duration(record)
    try:
        with Progress(action_word, total=duration, unit="s", stream=stream, quiet=quiet) as live:
            completed = run_tool(argv, progress=ffmpeg_progress_callback(live, duration))
        os.replace(str(temp), str(output_path))
    except (FfmpegError, ToolNotFound, OSError) as error:
        _remove(temp)
        stage = {"outcome": "failed", "error": str(error), "returncode": getattr(error, "returncode", None),
                 "stderr": _tail(getattr(error, "stderr", None)), "argv": argv}
        add_stage(record, "convert", stage)
        set_status(record, "failed")
        save_record(key, record)
        say("failed: '{}': {}".format(label, error), stream)
        return record["stages"]["convert"]
    finally:
        _remove(temp)
    elapsed = time.monotonic() - started

    first_source = _source_files(folder, record)[0][0]
    try:
        source_mtime = os.stat(str(first_source)).st_mtime_ns
        os.utime(str(output_path), ns=(os.stat(str(output_path)).st_atime_ns, source_mtime))
    except OSError as error:
        say("  could not copy the mtime of '{}' to '{}': {}".format(first_source.name, output_path.name, error),
            stream)
        add_flag(record, "mtime-not-set")

    output_size = output_path.stat().st_size
    source_size = (record.get("source") or {}).get("size")
    ratio = None
    if isinstance(source_size, int) and source_size > 0:
        ratio = round(float(output_size) / source_size, 4)
        if output_size > source_size and record.get("action") != "reencode":
            # A remux carries the same bitstreams; only the container changed, and the
            # fragment headers can make a small file grow. Not a failure (HANDOFF 5).
            add_flag(record, "remux-larger")
            say("  remuxed output is larger than the source ({} > {} bytes); container overhead, kept".format(
                output_size, source_size), stream)
        elif output_size > source_size:
            if not getattr(options, "keep_larger", False):
                _remove(output_path)
                stage = {"outcome": "larger", "output_size": output_size, "source_size": source_size,
                         "elapsed_s": round(elapsed, 3)}
                add_stage(record, "convert", stage)
                add_flag(record, "output-larger")
                set_status(record, "failed")
                save_record(key, record)
                say("  output is larger than the source ({} > {} bytes); removed '{}' "
                    "(--keep-larger keeps it)".format(output_size, source_size, output_path.name), stream)
                return record["stages"]["convert"]
            add_flag(record, "keep-larger")
            say("  output is larger than the source ({} > {} bytes); kept because of --keep-larger".format(
                output_size, source_size), stream)
    else:
        say("  source size unknown; the size rule could not be applied to '{}'".format(output_path.name), stream)

    sidecars_written, sidecar_errors = _write_sidecars(plan, stream, verbose)
    if sidecar_errors:
        add_flag(record, "sidecar-failed")

    par2_files = []
    par2_error = None
    redundancy = getattr(options, "par2", None)
    if redundancy is None:
        redundancy = plan.get("par2")
    if redundancy:
        if shutil.which("par2") is None:
            add_flag(record, "par2-skipped")
            say("  PAR2 asked for ({} %) but 'par2' is not on PATH; skipped for '{}'".format(
                redundancy, output_path.name), stream)
        else:
            try:
                par2_files = [path.name for path in write_par2(output_path, redundancy, progress)]
            except OSError as error:
                par2_error = str(error)
                add_flag(record, "par2-failed")
                say("  PAR2 failed for '{}': {}".format(output_path.name, error), stream)

    stage = {"outcome": "ok", "output_path": output_path.name, "part_path": temp.name,
             "elapsed_s": round(elapsed, 3), "ffmpeg_stderr": _tail(completed.stderr),
             "sidecars_written": sidecars_written, "par2_files": par2_files, "output_size": output_size,
             "source_size": source_size, "size_ratio": ratio}
    if sidecar_errors:
        stage["sidecar_errors"] = sidecar_errors
    if par2_error is not None:
        stage["par2_error"] = par2_error
    add_stage(record, "convert", stage)
    record["output"]["size"] = output_size
    record["output"]["fingerprint"] = cheap_fingerprint(output_path)
    set_status(record, "converted")
    save_record(key, record)
    say("  converted '{}' ({} bytes, {:.0f} % of the source) in {:.1f} s".format(
        output_path.name, output_size, 100.0 * ratio if ratio is not None else 0.0, elapsed), stream)
    _verify(tools, key, record, options, progress, stream)
    return record["stages"]["convert"]


# --- many files -------------------------------------------------------------------------


class _LineStream(object):
    """A stream that writes each write() call whole under one lock and never claims to be a TTY.

    With --jobs above 1 every say() line and every non-TTY progress line is
    one write() call, so lines from different threads never interleave.
    """

    def __init__(self, target):
        self.target = target
        self.lock = threading.Lock()
        self.encoding = getattr(target, "encoding", None)

    def write(self, text):
        with self.lock:
            self.target.write(text)
            self.target.flush()

    def flush(self):
        pass

    def isatty(self):
        return False


def _collect(paths, stream, problems):
    """[(key, folder, record)] for every record under the PATHs, each once."""
    found = []
    seen = set()

    def on_error(path, error):
        say("unreadable record '{}': {}".format(path, error), stream)
        problems.append(str(path))

    for given in paths:
        root = Path(os.path.abspath(os.fspath(given)))
        if root.is_dir():
            entries = iter_records(root, on_error=on_error)
        else:
            try:
                record = load_record(root)
            except RecordError as error:
                on_error(record_path(root), error)
                continue
            if record is None:
                say("'{}' has no record; run 'vidfunnel plan' on it first".format(root), stream)
                problems.append(str(root))
                continue
            entries = [(root.parent, record)]
        for folder, record in entries:
            key = record_key(folder, record)
            if str(key) in seen:
                continue
            seen.add(str(key))
            found.append((key, folder, record))
    return found


def _dry_run(entries, options, stream):
    total = len(entries)
    for position, (key, folder, record) in enumerate(entries, 1):
        output_path = output_path_for(record, folder)
        reason = skip_reason(record, output_path) or _refusal(record, folder, output_path)
        if reason is not None:
            say("[convert {}/{}] would skip '{}': {}".format(position, total, _label(record), reason), stream)
            continue
        plan = record["stages"]["plan"]
        argv = list(plan["argv"])
        argv[-1] = str(part_path(output_path))
        say("[convert {}/{}] would {} '{}' -> '{}'".format(position, total, ACTION_WORDS[record["action"]],
                                                          _label(record), output_path.name), stream)
        say(shlex.join(argv), stream)
        say("  then rename to '{}'".format(output_path), stream)
        for entry in plan.get("sidecars") or []:
            say(shlex.join(entry.get("argv") or []), stream)
        redundancy = getattr(options, "par2", None)
        if redundancy is None:
            redundancy = plan.get("par2")
        if redundancy:
            say("  then par2 create -r{} -n1 for '{}'".format(redundancy, output_path.name), stream)


def convert_paths(tools, paths, options, progress=None):
    """Convert every planned record under the PATHs; print lines and the summary; return the StageSummary."""
    stream, quiet, verbose = _stream_of(progress)
    jobs = max(1, int(getattr(options, "jobs", 1) or 1))
    out = _LineStream(stream if stream is not None else sys.stdout) if jobs > 1 else stream
    shared = types.SimpleNamespace(stream=out, quiet=quiet, verbose=verbose)
    summary = StageSummary("convert", OUTCOMES)
    problems = []
    say("scanning {} path(s) for records".format(len(paths)), out)
    entries = _collect(paths, out, problems)
    summary.add("failed", len(problems))
    wanted = []
    left_alone = {}
    for entry in entries:
        status = entry[2].get("status")
        if status == "planned" or status in DONE_STATUSES:
            wanted.append(entry)
        else:
            left_alone[status] = left_alone.get(status, 0) + 1
    if left_alone:
        say("left alone: {} record(s) not planned ({})".format(
            sum(left_alone.values()), ", ".join("{} {}".format(count, status)
                                                for status, count in sorted(left_alone.items(),
                                                                            key=lambda item: str(item[0])))), out)
    if getattr(options, "dry_run", False):
        _dry_run(wanted, options, out)
        say("convert: dry run, {} record(s) shown; nothing written".format(len(wanted)), out)
        return summary

    locks = []
    runnable = []
    locked_folders = {}
    try:
        for key, folder, record in wanted:
            name = str(folder)
            if name not in locked_folders:
                lock = FolderLock(folder, warn=lambda message: say(message, out))
                try:
                    lock.acquire()
                    locks.append(lock)
                    locked_folders[name] = True
                except LockHeld as error:
                    say("skipping folder '{}': {}".format(folder, error), out)
                    locked_folders[name] = False
            if locked_folders[name]:
                runnable.append((key, folder))
            else:
                summary.add("skipped")
        total = len(runnable)

        def work(position, key):
            try:
                record = load_record(key)
            except RecordError as error:
                say("failed: {}".format(error), out)
                return "failed"
            if record is None:
                say("failed: the record of '{}' disappeared".format(key.name), out)
                return "failed"
            stage = convert_one(tools, key, record, options, shared, position, total)
            outcome = stage.get("outcome")
            if outcome == "ok":
                if record.get("status") == "failed":
                    return "failed"
                return "verified" if record.get("status") == "verified" else "converted"
            if outcome == "larger":
                return "larger"
            if outcome == "skipped":
                return "skipped"
            return "failed"

        if jobs == 1:
            for position, (key, _folder) in enumerate(runnable, 1):
                summary.add(work(position, key))
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
                futures = [pool.submit(work, position, key) for position, (key, _folder) in enumerate(runnable, 1)]
                for future in futures:
                    summary.add(future.result())
    finally:
        for lock in locks:
            lock.release()
    say(summary.format("'{}/' under each folder".format(RECORD_DIR)), out)
    return summary


# --- command line -----------------------------------------------------------------------


def _positive_int(text):
    import argparse
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("'{}' is not a whole number".format(text))
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more, not {}".format(value))
    return value


def add_arguments(parser):
    parser.add_argument("paths", nargs="+", metavar="PATH",
                        help="files or folders whose planned records to convert (folders are walked)")
    parser.add_argument("--par2", type=_positive_int, default=None, metavar="N",
                        help="write PAR2 with N %% redundancy, overriding the planned value")
    parser.add_argument("--jobs", type=_positive_int, default=1, metavar="N",
                        help="files converted at once (default 1; x264 already uses every core)")
    parser.add_argument("--dry-run", action="store_true", help="print the commands only; write nothing")
    parser.add_argument("--keep-larger", action="store_true",
                        help="keep an output larger than its source (it is then not eligible for retirement)")
    return parser


def run(args):
    """`vidfunnel convert`: EXIT_OK, EXIT_ERROR when anything failed, EXIT_USAGE, EXIT_DEPS."""
    from vidfunnel.cli import EXIT_DEPS, EXIT_ERROR, EXIT_OK, EXIT_USAGE
    from vidfunnel.deps import DependencyError, require
    try:
        tools = require(args)
    except DependencyError as error:
        print("vidfunnel convert: {}".format(error), file=sys.stderr)
        return EXIT_DEPS
    missing = [path for path in args.paths if not os.path.exists(path)]
    if missing:
        for path in missing:
            print("vidfunnel convert: '{}' does not exist".format(path), file=sys.stderr)
        return EXIT_USAGE
    options = types.SimpleNamespace(par2=getattr(args, "par2", None), jobs=getattr(args, "jobs", 1),
                                    dry_run=bool(getattr(args, "dry_run", False)),
                                    keep_larger=bool(getattr(args, "keep_larger", False)),
                                    verbose=bool(getattr(args, "verbose", False)))
    progress = types.SimpleNamespace(stream=None, quiet=bool(getattr(args, "quiet", False)),
                                     verbose=options.verbose)
    summary = convert_paths(tools, args.paths, options, progress)
    failed = summary.counts.get("failed", 0) + summary.counts.get("larger", 0)
    return EXIT_ERROR if failed else EXIT_OK

# End of File #
