"""convert: run the planned ffmpeg commands to a hidden temp file, rename into place, write PAR2, verify.

HANDOFF.md sections 2.4, 2.5, 5 (the size rule), 6.3, 6.7 and 6.10.
`convert` runs exactly the argv that `plan` stored under stages.plan.argv,
with its last element (the output path) replaced by a hidden
'.NAME.<first 8 hex digits of the record id>.part' file in the same folder,
so two records can never write the same part file. On success the part file is renamed into place
(the interim '<stem>.vidfunnel.mp4' name when the source is itself an MP4),
the output gets the source's mtime, the subtitle sidecars are written the
same way, the size rule is applied, PAR2 is written when asked, and verify
runs on the result when vidfunnel.verify is present.

What convert writes into the record:

- stages.convert, on success: outcome 'ok', output_path (file name),
  part_path (file name), elapsed_s, ffmpeg_stderr (last 4000 characters),
  sidecars_written (file names), sidecar_errors (only when one failed),
  par2_files (file names), par2_redundancy (the N used, or None), par2_error
  (only when par2 failed), output_size,
  source_size, size_ratio (None when the source size is unknown);
- stages.convert on an ffmpeg failure: outcome 'failed', error,
  returncode, stderr (last 4000 characters), argv; status 'failed';
- a re-encode that came out larger than its source is kept like any
  other output (outcome 'ok', output_size and source_size as above) and
  gets the flag 'output-larger'; retire refuses it without --allow-larger.
  A remux that came out larger gets the informational flag 'remux-larger';
- stages.convert when the output name is taken by a file that is not
  ours: outcome 'output-collision', error; flag 'output-collision', status
  'failed';
- output.size and output.fingerprint (the cheap fingerprint after the
  mtime is set, so a later run can skip the file without verify having
  run), status 'converted' (or 'verified' once verify has run);
- flags 'output-larger', 'remux-larger', 'sidecar-failed', 'par2-skipped', 'par2-failed',
  'source-changed' (status 'source-changed' too) as they apply.

With --refit, convert revisits converted or verified re-encodes flagged
'output-larger' instead (see refit_one): one more encode at a higher CRF,
capped at 23 up to 576 lines and 24 above, with hqdn3d first when the source
is heavily compressed and blocky; the existing output is replaced only by a
smaller one that passes the identity, stream and duration checks, and is
otherwise kept, with the flag 'fit-capped'.

Before anything runs, planned records whose output names meet in one
folder (output.name or output.interim_name, compared case-insensitively)
are all refused: both source names are printed, neither record is
touched, and they are counted as 'collision' in the summary. A folder
whose lock is held, or cannot be created (a read-only folder), is skipped;
the second kind and any collision make run() exit with EXIT_ERROR.

The record is saved after every change of state, so an interrupted run
leaves a record that says what happened. A record that is not 'planned' is
refused without being touched, except that a converted or verified record
whose output still matches its fingerprint is reported as already converted.
"""

VERSION = "20261107"

import math
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

from vidfunnel.ffmpeg import FfmpegError, ToolNotFound, current_identity, forget_child, register_child
from vidfunnel.ffmpeg import run as run_tool
from vidfunnel.interrupt import Interrupted, child_interrupted, run_pool
from vidfunnel.interrupt import check as check_interrupted
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
OUTCOMES = ("converted", "verified", "skipped", "failed")
ACTION_WORDS = {"remux": "remux", "reencode": "re-encode"}
DONE_STATUSES = ("converted", "verified")

# --- refit (convert --refit) ---
# One CRF step of x264 -preset slower saved 11.4 to 12.5 % of size on the content
# measured for this rule (mandelbrot from an MPEG-4 q5 source: CRF 18 787647 bytes,
# 19 698048, 21 540167, 23 414027). A factor of 1.12 per step (a 10.7 % saving) is a
# round figure a little below that, so ceil(ln(ratio) / ln(1.12)) errs toward one step
# more rather than one too few. The caps below bound it; a refit that still comes out
# larger is kept as it is, flagged.
REFIT_STEP_LOG = math.log(1.12)
REFIT_CAP_SD = 23
REFIT_CAP_HD = 24
REFIT_SD_MAX_HEIGHT = 576
REFIT_DENOISE = "hqdn3d=2:1:2:3"
REFIT_OUTCOMES = ("refitted", "kept", "skipped", "failed")
REFIT_FLAG = "refitted"
FIT_CAPPED_FLAG = "fit-capped"
OUTPUT_LARGER_FLAG = "output-larger"
VIDEO_FILTER_OPTIONS = ("-vf", "-filter:v")


# --- names ------------------------------------------------------------------------------


def part_path(output_path, record_id):
    """The temp file convert writes to: same directory, '.<name>.<id8>.part' (hidden from Jellyfin and Finder).

    <id8> is the first 8 hex digits of the record id, so the part names of
    two records never meet even when their output names do.
    """
    digits = "".join(character for character in str(record_id or "") if character in "0123456789abcdefABCDEF")
    if len(digits) < 8:
        raise ValueError("record id '{}' has fewer than 8 hex digits".format(record_id))
    output_path = Path(output_path)
    return output_path.parent / ("." + output_path.name + "." + digits[:8].lower() + PART_SUFFIX)


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


def plan_ffmpeg_problem(record):
    """Why the ffmpeg at stages.plan.argv[0] is not the build the plan was made with, or None.

    Compares its version and configuration hash now (one '-version' call,
    vidfunnel.ffmpeg.current_identity) with stages.plan.ffmpeg. A record
    planned before plan wrote that field (20261107) gives None and runs as
    before. Call it once argv[0] is known to be an executable file.
    """
    plan = (record.get("stages") or {}).get("plan") or {}
    if "ffmpeg" not in plan:
        return None
    recorded = plan["ffmpeg"]
    path = plan["argv"][0]
    if not isinstance(recorded, dict):
        return "stages.plan.ffmpeg is not understood; plan again with --replan on this machine"
    try:
        current = current_identity(path)
    except (FfmpegError, ToolNotFound) as error:
        return "ffmpeg '{}' named in the plan does not run here ({}); plan again with --replan on this machine".format(
            path, error)
    if (current["version"] == recorded.get("version")
            and current["configuration_sha256"] == recorded.get("configuration_sha256")):
        return None
    return ("the plan was made with ffmpeg {} at '{}' (configuration {}), but '{}' here is ffmpeg {} "
            "(configuration {}); plan again with --replan on this machine".format(
                recorded.get("version") or "of unknown version", recorded.get("path"),
                str(recorded.get("configuration_sha256") or "unknown")[:12], current["path"],
                current["version"] or "of unknown version", current["configuration_sha256"][:12]))


def _refusal(record, folder, output_path):
    """Why a record cannot be converted as it stands, or None. Writes nothing; runs 'ffmpeg -version' once."""
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
    return plan_ffmpeg_problem(record)


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
    check_interrupted()
    process = subprocess.Popen(argv, cwd=str(output_path.parent), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                               encoding="utf-8", errors="replace")
    register_child(process)
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
            forget_child(process)
            reader.join()
            process.stdout.close()
    if process.returncode != 0:
        for partial in _par2_files(output_path):
            _remove(partial)
        if child_interrupted(process.returncode):
            raise Interrupted("par2 was stopped by the interrupt (status {})".format(process.returncode))
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


def _write_sidecars(plan, record_id, stream, verbose):
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
        temp = part_path(final, record_id)
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
    the folder. `options` carries par2 (override or None) and
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
    temp = part_path(output_path, record.get("id"))
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
            # Kept and flagged; retire refuses it without --allow-larger (HANDOFF 5).
            add_flag(record, "output-larger")
            say("  output is larger than the source ({} > {} bytes); kept and flagged 'output-larger' "
                "(retire will refuse it without --allow-larger)".format(output_size, source_size), stream)
    else:
        say("  source size unknown; the size rule could not be applied to '{}'".format(output_path.name), stream)

    sidecars_written, sidecar_errors = _write_sidecars(plan, record.get("id"), stream, verbose)
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
             "sidecars_written": sidecars_written, "par2_files": par2_files,
             "par2_redundancy": int(redundancy) if redundancy else None, "output_size": output_size,
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


# --- refit -------------------------------------------------------------------------------


def refit_selection(record):
    """None when convert --refit should try this record, else the reason it is skipped. Pure.

    Selected: status 'converted' or 'verified', action 'reencode', flag
    'output-larger', no flag 'fit-capped', and no refit stage (one whose
    outcome is 'failed' may be tried again).
    """
    if record.get("status") not in DONE_STATUSES:
        return "not converted"
    if record.get("action") != "reencode":
        return "not a re-encode"
    flags = record.get("flags") or []
    if FIT_CAPPED_FLAG in flags:
        return "already capped"
    previous = (record.get("stages") or {}).get("refit")
    if previous is not None and previous.get("outcome") != "failed":
        return "already refitted"
    if OUTPUT_LARGER_FLAG not in flags:
        return "not larger"
    return None


def parse_crf(argv):
    """The integer after '-crf' in an argv, or None when there is none or it is not a whole number. Pure."""
    argv = list(argv or [])
    for position, item in enumerate(argv[:-1]):
        if item == "-crf":
            try:
                return int(argv[position + 1])
            except (TypeError, ValueError):
                return None
    return None


def refit_cap(height):
    """REFIT_CAP_SD (23) up to 576 lines or when the height is unknown, REFIT_CAP_HD (24) above. Pure."""
    if not height or height <= REFIT_SD_MAX_HEIGHT:
        return REFIT_CAP_SD
    return REFIT_CAP_HD


def refit_crf(base_crf, ratio, height):
    """(crf, cap, steps): base_crf + ceil(ln(ratio) / 0.14), no higher than the cap for the height. Pure.

    ratio is output size over source size. A ratio of 1 or below gives 0
    steps (the caller treats a CRF not above base_crf as nothing to do).
    """
    cap = refit_cap(height)
    if ratio is None or ratio <= 1.0:
        steps = 0
    else:
        steps = int(math.ceil(math.log(ratio) / REFIT_STEP_LOG))
    return min(base_crf + steps, cap), cap, steps


def refit_denoise(record):
    """(denoise, reason): hqdn3d only for a 'heavy' size class with blockiness at or above BLOCKINESS_HIGH. Pure."""
    from vidfunnel.analyze import BLOCKINESS_HIGH
    analysis = (record.get("stages") or {}).get("analyze") or {}
    size = analysis.get("size_class")
    blocks = analysis.get("blockiness")
    if not isinstance(size, dict):
        return False, "no size class in the record (planned before 20261107); no denoise"
    if size.get("class") != "heavy":
        return False, "size class '{}', not 'heavy'; no denoise".format(size.get("class"))
    score = blocks.get("score") if isinstance(blocks, dict) else None
    if score is None:
        return False, "size class 'heavy' but blockiness not measured; no denoise"
    if score < BLOCKINESS_HIGH:
        return False, "size class 'heavy', blockiness {} below {}; no denoise".format(score, BLOCKINESS_HIGH)
    return True, "size class 'heavy' and blockiness {} at or above {}: {}".format(score, BLOCKINESS_HIGH,
                                                                                REFIT_DENOISE)


def refit_filter_chain(chain, denoise, cadence_filter=None):
    """The video filter chain for a refit: hqdn3d first, or right after the cadence filter. Pure.

    A chain that starts with the record's cadence filter (fieldmatch,decimate
    or bwdif) gets hqdn3d after it, so the field matching and deinterlacing
    still see the source's fields; otherwise hqdn3d goes first. Without
    denoise the chain is returned unchanged (None stays None).
    """
    if not denoise:
        return chain
    if not chain:
        return REFIT_DENOISE
    if cadence_filter and (chain == cadence_filter or chain.startswith(cadence_filter + ",")):
        rest = chain[len(cadence_filter):]
        return cadence_filter + "," + REFIT_DENOISE + rest
    return REFIT_DENOISE + "," + chain


def refit_argv(argv, crf, denoise, output, cadence_filter=None):
    """(argv, filter chain or None): the planned argv with -crf, the video filter chain and the output replaced. Pure.

    Everything else (inputs, maps, identity tag, movflags) is kept. With
    denoise and no '-vf'/'-filter:v' in the argv, '-vf hqdn3d=...' goes in
    before '-c:v'. Raises ValueError when the argv has no '-crf', or needs a
    filter chain and has no '-c:v' to put it before.
    """
    argv = list(argv)
    try:
        position = argv.index("-crf")
    except ValueError:
        raise ValueError("the planned command has no -crf")
    if position + 1 >= len(argv) - 1:
        raise ValueError("the planned command has no value after -crf")
    argv[position + 1] = str(int(crf))
    chain = None
    filter_at = None
    for index, item in enumerate(argv[:-1]):
        if item in VIDEO_FILTER_OPTIONS:
            filter_at = index + 1
            chain = argv[filter_at]
            break
    new_chain = refit_filter_chain(chain, denoise, cadence_filter)
    if filter_at is not None:
        argv[filter_at] = new_chain
    elif new_chain:
        if "-c:v" not in argv:
            raise ValueError("the planned command has no -c:v to put the video filter chain before")
        at = argv.index("-c:v")
        argv[at:at] = ["-vf", new_chain]
    argv[-1] = os.fspath(output)
    return argv, new_chain


def _refit_disk_problem(folder, record, output_path):
    """(problem or None, output_size, source_size): the files on disk against the record's convert stage."""
    convert = (record.get("stages") or {}).get("convert") or {}
    source_size = convert.get("source_size") or (record.get("source") or {}).get("size")
    recorded_size = convert.get("output_size")
    if not isinstance(source_size, int) or source_size <= 0:
        return "the source size is unknown", None, None
    try:
        current = cheap_fingerprint(output_path)
    except OSError:
        return "output '{}' is missing".format(output_path.name), None, source_size
    size = current["size"]
    if recorded_size is not None and size != recorded_size:
        return ("output '{}' is {} bytes, the record says {}; it changed since convert".format(
            output_path.name, size, recorded_size), size, source_size)
    recorded = (record.get("output") or {}).get("fingerprint")
    if recorded and not fingerprint_matches(recorded, current):
        return "output '{}' no longer matches its recorded fingerprint".format(output_path.name), size, source_size
    changed = source_change(folder, record)
    if changed is not None:
        return "source changed since plan: {}".format(changed), size, source_size
    return None, size, source_size


def _refit_plan_problem(record, output_path):
    """Why the planned argv cannot be the base of a refit, or None."""
    argv = ((record.get("stages") or {}).get("plan") or {}).get("argv")
    if not isinstance(argv, list) or len(argv) < 4 or argv[-3:-1] != ["-f", "mp4"]:
        return "stages.plan.argv does not end in '-f mp4 <output>'"
    if os.path.abspath(argv[-1]) != os.path.abspath(str(output_path)):
        return "the planned command writes '{}', not '{}'".format(argv[-1], output_path)
    if not (os.path.isfile(argv[0]) and os.access(argv[0], os.X_OK)):
        return "ffmpeg '{}' named in the plan does not exist here".format(argv[0])
    if parse_crf(argv) is None:
        return "the planned command has no -crf"
    return plan_ffmpeg_problem(record)


def refit_checks(tools, record, path):
    """None when the refit output at `path` passes the identity, stream and duration checks, else the reason.

    The checks are verify's own (check_identity, compare_streams,
    compare_durations against the source video). The duration check also
    passes when it misses by no more than the existing output did in its
    last verify, so a source whose container duration was already off is
    not held against the refit.
    """
    from vidfunnel.probe import probe_file
    from vidfunnel.verify import (
        check_identity,
        compare_durations,
        compare_streams,
        duration_tolerance,
        expected_streams,
        output_video_duration,
        read_tag,
        source_video_duration,
    )
    try:
        probe = probe_file(tools, path)
    except FfmpegError as error:
        return "ffprobe cannot read the refit output: {}".format(error)
    identity = check_identity(record, read_tag(probe))
    if not identity["ok"]:
        return "identity tag: {}".format(identity["reason"])
    streams = compare_streams(expected_streams(record), probe)
    if not streams["ok"]:
        return "streams {} differ from the expected {}".format(streams["actual"], streams["expected"])
    source_seconds, _kind = source_video_duration(record)
    output_seconds, _kind = output_video_duration(probe)
    duration = compare_durations(source_seconds, output_seconds,
                                 duration_tolerance((probe.get("video") or {}).get("fps")))
    if not duration["ok"]:
        earlier = ((record.get("stages") or {}).get("verify") or {}).get("duration") or {}
        if (duration.get("delta") is None or earlier.get("delta") is None
                or abs(duration["delta"]) > abs(earlier["delta"]) + 1e-6):
            return "video duration {} differs from the source's {} by more than {} s".format(
                duration.get("output"), duration.get("source"), round(duration.get("tolerance") or 0.0, 4))
    return None


def refit_plan(record, folder):
    """(plan dict or None, skip reason or None) for one record; reads the files, writes nothing.

    The plan dict: output_path, part, argv, base_crf, crf, cap, steps,
    denoise, denoise_reason, filters, ratio, output_size, source_size,
    height.
    """
    reason = refit_selection(record)
    if reason is not None:
        return None, reason
    output_path = output_path_for(record, folder)
    problem = _refit_plan_problem(record, output_path)
    if problem is not None:
        return None, problem
    problem, output_size, source_size = _refit_disk_problem(folder, record, output_path)
    if problem is not None:
        return None, problem
    ratio = float(output_size) / source_size
    if ratio <= 1.0:
        return None, "not larger (output {} bytes, source {} bytes on disk)".format(output_size, source_size)
    argv = record["stages"]["plan"]["argv"]
    base_crf = parse_crf(argv)
    height = (((record.get("stages") or {}).get("probe") or {}).get("video") or {}).get("height")
    crf, cap, steps = refit_crf(base_crf, ratio, height)
    if crf <= base_crf:
        return None, "no higher CRF would help (CRF {} is at the cap {})".format(base_crf, cap)
    denoise, denoise_reason = refit_denoise(record)
    cadence_filter = ((((record.get("stages") or {}).get("analyze") or {}).get("cadence") or {}).get("result")
                      or {}).get("filter")
    part = part_path(output_path, record.get("id"))
    try:
        new_argv, chain = refit_argv(argv, crf, denoise, part, cadence_filter)
    except ValueError as error:
        return None, str(error)
    return {"output_path": output_path, "part": part, "argv": new_argv, "base_crf": base_crf, "crf": crf,
            "cap": cap, "steps": steps, "denoise": denoise, "denoise_reason": denoise_reason, "filters": chain,
            "ratio": ratio, "output_size": output_size, "source_size": source_size, "height": height}, None


def _refit_words(plan):
    return "CRF {} -> {}{}, {}, ratio {:.2f}".format(
        plan["base_crf"], plan["crf"], " (cap)" if plan["crf"] == plan["cap"] else "",
        "hqdn3d" if plan["denoise"] else "no denoise", plan["ratio"])


def refit_one(tools, source, record, options, progress=None, position=None, total=None):
    """One refit attempt for one record; returns (outcome, reason): 'refitted', 'kept', 'skipped' or 'failed'.

    'refitted': the refit output was smaller than the existing output and
    passed refit_checks; it replaced the output (os.replace of the part
    file), the record went back to 'converted' with its output hash, score
    and verify stage cleared (the verify stage moves to verify_history),
    stage 'refit' with outcome 'replaced', flag 'refitted', 'output-larger'
    removed only when the new output is not larger than the source, PAR2
    written again when convert wrote it; then verify runs as after convert.
    'kept': the refit output was not smaller or failed a check; the part file
    is removed, the output and record are untouched except for stage 'refit'
    (outcome 'kept-existing', reason) and the flag 'fit-capped'.
    'failed': ffmpeg failed; stage 'refit' with outcome 'failed' and the
    error, no 'fit-capped', so a later --refit run tries again (the failed
    stage then moves to stages.refit_history).
    """
    stream, quiet, verbose = _stream_of(progress)
    verbose = verbose or bool(getattr(options, "verbose", False))
    key = Path(os.path.abspath(os.fspath(source)))
    folder = key.parent
    label = _label(record)
    plan, reason = refit_plan(record, folder)
    if plan is None:
        say("skipping '{}': {}".format(label, reason), stream)
        return "skipped", reason
    counter = "[refit {}/{}]".format(position, total) if position is not None and total else "[refit]"
    say("{} '{}': {}".format(counter, label, _refit_words(plan)), stream)
    say("  {}".format(plan["denoise_reason"]), stream)
    if verbose:
        say(shlex.join(plan["argv"]), stream)

    stages = record.setdefault("stages", {})
    if "refit" in stages:
        stages.setdefault("refit_history", []).append(stages.pop("refit"))
    attempt = {"crf": plan["crf"], "base_crf": plan["base_crf"], "cap": plan["cap"], "steps": plan["steps"],
               "denoise": plan["denoise"], "denoise_reason": plan["denoise_reason"], "filters": plan["filters"],
               "previous_output_size": plan["output_size"], "previous_ratio": round(plan["ratio"], 4),
               "source_size": plan["source_size"], "output_size": None, "ratio": None, "elapsed_s": None,
               "argv": plan["argv"]}
    temp = plan["part"]
    output_path = plan["output_path"]
    if _remove(temp):
        say("  removed a stale '{}' from an earlier run".format(temp.name), stream)
    started = time.monotonic()
    duration = expected_duration(record)
    try:
        return _refit_attempt(tools, key, record, options, progress, plan, attempt, label, started, duration)
    finally:
        # Gone after a replace or a removal; this catches an exception between the encode and either.
        _remove(temp)


def _refit_attempt(tools, key, record, options, progress, plan, attempt, label, started, duration):
    """The encode, the comparison and the replace or keep of refit_one; see there."""
    stream, quiet, _verbose = _stream_of(progress)
    folder = key.parent
    stages = record.setdefault("stages", {})
    temp = plan["part"]
    output_path = plan["output_path"]
    try:
        with Progress("refit", total=duration, unit="s", stream=stream, quiet=quiet) as live:
            completed = run_tool(plan["argv"], progress=ffmpeg_progress_callback(live, duration))
    except (FfmpegError, ToolNotFound, OSError) as error:
        _remove(temp)
        attempt["elapsed_s"] = round(time.monotonic() - started, 3)
        add_stage(record, "refit", {"attempt": attempt, "outcome": "failed", "error": str(error),
                                    "stderr": _tail(getattr(error, "stderr", None))})
        save_record(key, record)
        say("failed: refit of '{}': {}; the existing output is kept".format(label, error), stream)
        return "failed", str(error)
    attempt["elapsed_s"] = round(time.monotonic() - started, 3)
    attempt["ffmpeg_stderr"] = _tail(completed.stderr)

    try:
        new_size = temp.stat().st_size
    except OSError as error:
        new_size = None
        problem = "the refit output cannot be read: {}".format(error)
    else:
        attempt["output_size"] = new_size
        attempt["ratio"] = round(float(new_size) / plan["source_size"], 4)
        if new_size >= plan["output_size"]:
            problem = "the refit output ({} bytes) is not smaller than the existing output ({} bytes)".format(
                new_size, plan["output_size"])
        else:
            problem = refit_checks(tools, record, temp)
    result_words = "{} -> {}".format(_refit_words(plan),
                                     "?" if attempt["ratio"] is None else "{:.2f}".format(attempt["ratio"]))
    if problem is not None:
        _remove(temp)
        add_stage(record, "refit", {"attempt": attempt, "outcome": "kept-existing", "reason": problem})
        add_flag(record, FIT_CAPPED_FLAG)
        save_record(key, record)
        say("  refit '{}': {}; {}; kept the existing output, flagged '{}'".format(
            label, result_words, problem, FIT_CAPPED_FLAG), stream)
        return "kept", problem

    try:
        os.replace(str(temp), str(output_path))
    except OSError as error:
        _remove(temp)
        add_stage(record, "refit", {"attempt": attempt, "outcome": "failed",
                                    "error": "could not replace '{}': {}".format(output_path.name, error)})
        save_record(key, record)
        say("failed: refit of '{}': could not replace '{}': {}".format(label, output_path.name, error), stream)
        return "failed", str(error)

    # The file on disk is now the refit. Everything that describes it goes into the
    # record and is saved before the slow PAR2 step, so an interrupt there leaves a
    # record that says what happened (a convert PAR2 set is then stale, see below).
    first_source = _source_files(folder, record)[0][0]
    try:
        source_mtime = os.stat(str(first_source)).st_mtime_ns
        os.utime(str(output_path), ns=(os.stat(str(output_path)).st_atime_ns, source_mtime))
    except OSError as error:
        say("  could not copy the mtime of '{}' to '{}': {}".format(first_source.name, output_path.name, error),
            stream)
        add_flag(record, "mtime-not-set")

    from vidfunnel.verify import _archive_previous
    _archive_previous(record)
    record.pop("score", None)
    output = record.setdefault("output", {})
    output.pop("sha256", None)
    add_stage(record, "refit", {"attempt": attempt, "outcome": "replaced"})
    refit_stage = stages["refit"]
    add_flag(record, REFIT_FLAG)
    if new_size <= plan["source_size"]:
        record["flags"] = [flag for flag in record.get("flags") or [] if flag != OUTPUT_LARGER_FLAG]
    output["size"] = new_size
    output["fingerprint"] = cheap_fingerprint(output_path)
    set_status(record, "converted")
    convert = stages.get("convert") or {}
    redundancy = getattr(options, "par2", None)
    if redundancy is None and convert.get("par2_files"):
        redundancy = convert.get("par2_redundancy")
    stale_par2 = _par2_files(output_path)
    if stale_par2:
        # Written for the output this one replaced; they can no longer repair anything.
        refit_stage["par2_stale"] = [path.name for path in stale_par2]
        add_flag(record, "par2-stale")
    save_record(key, record)

    if redundancy:
        if shutil.which("par2") is None:
            for path in stale_par2:
                _remove(path)
            say("  PAR2 for '{}' is out of date and 'par2' is not on PATH; the stale set is removed, "
                "flagged 'par2-stale'".format(output_path.name), stream)
        else:
            try:
                refit_stage["par2_files"] = [path.name for path in write_par2(output_path, redundancy, progress)]
            except OSError as error:
                refit_stage["par2_error"] = str(error)
                add_flag(record, "par2-failed")
                say("  PAR2 failed for '{}': {}".format(output_path.name, error), stream)
            else:
                record["flags"] = [flag for flag in record.get("flags") or [] if flag != "par2-stale"]
            save_record(key, record)
    say("  refit '{}': {}; replaced '{}' ({} bytes, was {}){}".format(
        label, result_words, output_path.name, new_size, plan["output_size"],
        "" if new_size <= plan["source_size"] else "; still larger than the source, '{}' kept".format(
            OUTPUT_LARGER_FLAG)), stream)
    _verify(tools, key, record, options, progress, stream)
    if record.get("status") == "failed":
        return "failed", "verify failed after the refit"
    return "refitted", None


def _refit_dry_run(entries, stream):
    total = len(entries)
    shown = 0
    for position, (key, folder, record) in enumerate(entries, 1):
        plan, reason = refit_plan(record, folder)
        if plan is None:
            say("[refit {}/{}] would skip '{}': {}".format(position, total, _label(record), reason), stream)
            continue
        shown += 1
        say("[refit {}/{}] would refit '{}': {}".format(position, total, _label(record), _refit_words(plan)), stream)
        say("  {}".format(plan["denoise_reason"]), stream)
        say(shlex.join(plan["argv"]), stream)
        say("  then, if smaller than the existing output and it passes the checks, rename over '{}'".format(
            plan["output_path"]), stream)
    return shown


def refit_paths(tools, paths, options, progress=None):
    """convert --refit over the records under the PATHs; print lines, skip reasons and the summary.

    Returns the StageSummary (counts refitted, kept, skipped, failed, with
    `lock_errors` and `skip_reasons`, a {reason: count} dict).

    Ctrl+C with --jobs: as in convert_paths, run_pool sets
    vidfunnel.interrupt.interrupted on this thread, the workers' ffmpeg runs
    raise Interrupted, and refit_one lets it through its 'failed' handler
    and removes its part file on the way out, so the record of an
    interrupted refit is left as it was.
    """
    stream, quiet, verbose = _stream_of(progress)
    jobs = max(1, int(getattr(options, "jobs", 1) or 1))
    out = _LineStream(stream if stream is not None else sys.stdout) if jobs > 1 else stream
    shared = types.SimpleNamespace(stream=out, quiet=quiet, verbose=verbose)
    summary = StageSummary("convert --refit", REFIT_OUTCOMES)
    summary.lock_errors = []
    summary.skip_reasons = {}
    problems = []
    say("scanning {} path(s) for records to refit".format(len(paths)), out)
    entries = _collect(paths, out, problems)
    summary.add("failed", len(problems))
    if getattr(options, "dry_run", False):
        shown = _refit_dry_run(entries, out)
        say("convert --refit: dry run, {} record(s) would be refitted; nothing written".format(shown), out)
        return summary

    def skipped(reason):
        summary.add("skipped")
        short = reason if reason in ("not converted", "not a re-encode", "already capped", "already refitted",
                                     "not larger", "folder locked") else "other (reason printed above)"
        summary.skip_reasons[short] = summary.skip_reasons.get(short, 0) + 1

    selected = []
    for key, folder, record in entries:
        reason = refit_selection(record)
        if reason is None:
            selected.append((key, folder))
        else:
            skipped(reason)

    locks = []
    runnable = []
    locked_folders = {}
    try:
        for key, folder in selected:
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
                except OSError as error:
                    say("skipping folder '{}': cannot take its lock: {}".format(folder, error), out)
                    locked_folders[name] = False
                    summary.lock_errors.append(name)
            if locked_folders[name]:
                runnable.append(key)
            else:
                skipped("folder locked")
        total = len(runnable)

        def work(position, key):
            try:
                record = load_record(key)
            except RecordError as error:
                say("failed: {}".format(error), out)
                return "failed", None
            if record is None:
                say("failed: the record of '{}' disappeared".format(key.name), out)
                return "failed", None
            return refit_one(tools, key, record, options, shared, position, total)

        results = []
        if jobs == 1:
            for position, key in enumerate(runnable, 1):
                results.append(work(position, key))
        else:
            results = [None] * len(runnable)

            def refit_done(index, value):
                results[index] = value

            run_pool(jobs, [lambda position=position, key=key: work(position, key)
                            for position, key in enumerate(runnable, 1)], refit_done)
        for outcome, reason in results:
            if outcome == "skipped":
                skipped(reason)
            else:
                summary.add(outcome)
    finally:
        for lock in locks:
            lock.release()
    if summary.skip_reasons:
        say("skipped: {}".format(", ".join("{} {}".format(count, reason)
                                           for reason, count in sorted(summary.skip_reasons.items()))), out)
    say(summary.format("'{}/' under each folder".format(RECORD_DIR)), out)
    return summary


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
        argv[-1] = str(part_path(output_path, record.get("id")))
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


def _shared_outputs(runnable, stream):
    """(kept, refused): runnable [(key, folder)] without the planned records whose output names meet.

    Two planned records in one folder whose output.name or output.interim_name
    are equal (case-insensitively) would write the same file; all of them are
    refused, their source names printed, and their records left untouched.
    refused is the number of records taken out. A record that cannot be read
    here stays in `kept`, where the run reports it.
    """
    claims = {}
    labels = {}
    for key, folder in runnable:
        try:
            record = load_record(key)
        except RecordError:
            continue
        if record is None or record.get("status") != "planned":
            continue
        labels[str(key)] = _label(record)
        output = record.get("output") or {}
        names = set()
        for name in (output.get("name"), output.get("interim_name")):
            if isinstance(name, str) and name:
                names.add(name.casefold())
        for name in sorted(names):
            claims.setdefault((str(folder), name), []).append(key)
    refused = set()
    for (folder, name), keys in sorted(claims.items()):
        if len(keys) < 2:
            continue
        say("refusing {} in '{}': they would all write '{}'; none of them is converted; plan again after "
            "renaming or removing one".format(" and ".join("'{}'".format(labels[str(key)]) for key in keys),
                                             folder, name), stream)
        refused.update(str(key) for key in keys)
    kept = [(key, folder) for key, folder in runnable if str(key) not in refused]
    return kept, len(refused)


def convert_paths(tools, paths, options, progress=None):
    """Convert every planned record under the PATHs; print lines and the summary; return the StageSummary.

    The summary carries `lock_errors` (folders whose lock could not be
    created) next to its counts; refused output collisions are counted as
    'collision'.

    Ctrl+C with --jobs: vidfunnel.interrupt.interrupted is set by run_pool on
    this thread when KeyboardInterrupt arrives and read by every ffmpeg and
    par2 start and exit in the workers (and the hash loop of verify), which
    then raise Interrupted. convert_one's handlers that record 'failed' do
    not catch it and its finally removes the part file, so the record of an
    interrupted item keeps the last state it was saved in, as after Ctrl+C
    without --jobs; items not yet started are not started.
    """
    if getattr(options, "refit", False):
        return refit_paths(tools, paths, options, progress)
    stream, quiet, verbose = _stream_of(progress)
    jobs = max(1, int(getattr(options, "jobs", 1) or 1))
    out = _LineStream(stream if stream is not None else sys.stdout) if jobs > 1 else stream
    shared = types.SimpleNamespace(stream=out, quiet=quiet, verbose=verbose)
    summary = StageSummary("convert", OUTCOMES)
    summary.lock_errors = []
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
                except OSError as error:
                    say("skipping folder '{}': cannot take its lock: {}".format(folder, error), out)
                    locked_folders[name] = False
                    summary.lock_errors.append(name)
            if locked_folders[name]:
                runnable.append((key, folder))
            else:
                summary.add("skipped")
        runnable, collided = _shared_outputs(runnable, out)
        if collided:
            summary.add("collision", collided)
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
            if outcome == "skipped":
                return "skipped"
            return "failed"

        if jobs == 1:
            for position, (key, _folder) in enumerate(runnable, 1):
                summary.add(work(position, key))
        else:
            run_pool(jobs, [lambda position=position, key=key: work(position, key)
                            for position, (key, _folder) in enumerate(runnable, 1)],
                     lambda _index, outcome: summary.add(outcome))
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
    parser.add_argument("paths", nargs="*", metavar="PATH",
                        help="files or folders whose planned records to convert "
                        "(folders are walked; default: the current directory)")
    parser.add_argument("--par2", type=_positive_int, default=None, metavar="N",
                        help="write PAR2 with N %% redundancy, overriding the planned value")
    parser.add_argument("--jobs", type=_positive_int, default=1, metavar="N",
                        help="files converted at once (default 1; x264 already uses every core)")
    parser.add_argument("--dry-run", action="store_true", help="print the commands only; write nothing")
    parser.add_argument("--refit", action="store_true",
                        help="re-encode converted outputs flagged 'output-larger' once more at a higher CRF "
                        "(capped at 23 for SD, 24 above), keeping the smaller result")
    return parser


def run(args):
    """`vidfunnel convert`: EXIT_OK, EXIT_ERROR when anything failed, collided or could not be locked, EXIT_USAGE,
    EXIT_DEPS."""
    from vidfunnel.cli import EXIT_DEPS, EXIT_ERROR, EXIT_OK, EXIT_USAGE, paths_or_current_directory
    from vidfunnel.deps import DependencyError, require
    paths_or_current_directory(args, "convert")
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
                                    verbose=bool(getattr(args, "verbose", False)),
                                    refit=bool(getattr(args, "refit", False)))
    progress = types.SimpleNamespace(stream=None, quiet=bool(getattr(args, "quiet", False)),
                                     verbose=options.verbose)
    summary = convert_paths(tools, args.paths, options, progress)
    failed = summary.counts.get("failed", 0) + summary.counts.get("collision", 0)
    return EXIT_ERROR if failed or getattr(summary, "lock_errors", None) else EXIT_OK

# End of File #
