"""convert: render the ffmpeg commands of planned records, run them to a hidden temp file, rename, PAR2, verify.

HANDOFF.md sections 2.4, 2.5, 5 (the size rule), 6.3, 6.7 and 6.10.
`convert` renders each command at convert time (vidfunnel.plan.render_record)
from the record, the folder the record sits in and this machine's ffmpeg
(tools.ffmpeg: --ffmpeg, VIDFUNNEL_FFMPEG or PATH). It never runs or trusts
the command plan stored under stages.plan.argv, which is a preview, also in
records written before this rule. Before rendering, the values the rendering
reads are checked for the forms analyze writes (plan.record_value_problem),
and before running, this ffmpeg's capability listing is checked for every
encoder (video, audio and subtitle), filter, bitstream filter, input format
and output format the command and each subtitle sidecar command name
(capability_problem); a record that fails either is skipped, counted as
skipped, and left untouched, with a reason that names the field or the
missing item and the ffmpeg's path.

The command's last element (the output path) is a hidden
'.NAME.<first 8 hex digits of the record id>.part' file in the same folder,
so two records can never write the same part file. On success the part file is renamed into place
(the interim '<stem>.vidfunnel.mp4' name when the source is itself an MP4,
or for any source planned with --mark-outputs, whose sidecars carry interim names too),
the output gets the source's mtime, the subtitle sidecars are written the
same way, the size rule is applied, PAR2 is written when asked, and verify
runs on the result when vidfunnel.verify is present.

What convert writes into the record:

- stages.convert, on success: outcome 'ok', output_path (file name),
  part_path (file name), elapsed_s, ffmpeg_stderr (last 4000 characters),
  sidecars_written (file names), sidecar_errors (only when one failed),
  par2_files (file names), par2_redundancy (the N used, or None), par2_error
  (only when par2 failed), output_size,
  source_size, size_ratio (None when the source size is unknown), and what
  ran: argv (the command, ending in the part file), sidecar_argvs (each
  sidecar command started, ending in its part file), ffmpeg ({'path',
  'version', 'configuration_sha256'}, vidfunnel.ffmpeg.current_identity) and
  version (the vidfunnel version that rendered the commands);
- stages.convert on an ffmpeg failure: outcome 'failed', error,
  returncode, stderr (last 4000 characters), argv, sidecar_argvs (empty),
  ffmpeg, version; status 'failed';
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
- flags 'output-larger', 'remux-larger', 'sidecar-failed', 'par2-skipped', 'par2-failed'
  as they apply;
- stages.convert.source_mtime_differs, [{'name', 'recorded', 'seen'}], and
  source_sha256_checked (whether the whole source was read) when a source
  file's modification time is not the one recorded at plan.

The source is checked by record.check_source before anything is written:
size and first and last MiB as recorded, and each time the recorded one or
a coarser view of it, or else the whole source read once with the recorded
SHA-256. A source that changed is skipped (counted skipped) and its record
left as it is, with a reason naming the file, what differs and the command
that plans it again. Before 20261109.3 convert set the status and flag
'source-changed' instead; records carrying them are read as before.

With --refit, convert revisits converted or verified re-encodes flagged
'output-larger' instead (see refit_one): one more encode at a higher CRF,
capped at 23 up to 576 lines and 24 above, with hqdn3d first when the source
is heavily compressed and blocky; the existing output is replaced only by a
smaller one that passes the identity, stream and duration checks, and is
otherwise kept, with the flag 'fit-capped'. The refit command is rendered
like convert's, with the same checks; the CRF it raises is the one the
existing output was made with (refit_base_crf).

A folder's lock is taken when its first item that needs one starts and
released when its last item has ended (vidfunnel.turns); with --jobs N at
most N folders are held at once. A record that is already converted, its
output unchanged, is reported without a lock, so a finished folder is
never locked. When a folder is entered, under its lock, the folder's
records collected at the scan are read as they are on disk at that moment
(a record another run writes after the scan is not part of the check), and
planned records whose output names meet in it (output.name or
output.interim_name, compared case-insensitively) are all refused: both
source names are printed, neither record is touched, and they are counted
as 'collision' in the summary. A folder whose lock is
held, or cannot be created (a read-only folder), is skipped for the run;
the second kind and any collision make run() exit with EXIT_ERROR.

The record is saved after every change of state, so an interrupted run
leaves a record that says what happened. A record that is not 'planned' is
refused without being touched, except that a converted or verified record
whose output still matches its fingerprint (content only, the time is not
compared) is reported as already converted.
"""

VERSION = "20261110"

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

import vidfunnel
from vidfunnel.ffmpeg import FfmpegError, ToolNotFound, current_identity, forget_child, register_child
from vidfunnel.ffmpeg import capabilities as ffmpeg_capabilities
from vidfunnel.ffmpeg import run as run_tool
from vidfunnel.plan import PlanError, record_key_problem, record_name_problem, record_value_problem, render_record
from vidfunnel.interrupt import Interrupted, child_interrupted, drain_after_item, drain_before_next, run_pool
from vidfunnel.interrupt import check as check_interrupted
from vidfunnel.progress import Progress, StageSummary, ffmpeg_progress_callback, item_tag, say, tagged
from vidfunnel.stopfile import forget_item, raise_if_stopped_now, remember_item
from vidfunnel.turns import DRAINING, HELD, FolderTurns, not_started
from vidfunnel.record import (
    RECORD_DIR,
    LockLost,
    RecordError,
    add_flag,
    add_stage,
    cheap_fingerprint,
    check_source,
    fingerprint_matches,
    ensure_lock,
    held_lock_fds,
    iter_records,
    load_record,
    lock_problem,
    mtime_difference,
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


def _source_path(folder, record):
    """The path 'vidfunnel plan' takes for this record's source: the file, a VOB group's first member, a DVD folder."""
    return Path(folder) / str((record.get("source") or {}).get("name"))


def plan_again_advice(folder, record):
    """The command that plans this record's changed source again, and only it: 'vidfunnel plan' on its path."""
    return "plan it again with: vidfunnel plan '{}'".format(_source_path(folder, record))


def changed_after_convert_advice(folder, record, output_name):
    """What resolves a source that changed after its output was made: move the output away, then plan the source."""
    return ("the output '{}' was made from the earlier content; to convert the source as it is now, move '{}' "
            "away and run: vidfunnel plan '{}'".format(output_name, output_name, _source_path(folder, record)))


def source_change(folder, record, advice, stream=None, quiet=False):
    """(reason, notes) for the record's source files against the fingerprints taken at plan (record.check_source).

    reason is None when the source is unchanged: size and first and last MiB
    as recorded, and each time the recorded one or a coarser view of it, or
    else the whole source read once with the SHA-256 the record holds. Else
    it is 'source changed since plan: ' with what differs and `advice` (the
    command that resolves it), or says which file cannot be read. notes is {}
    or, when a time differs, {'source_mtime_differs': [{'name', 'recorded',
    'seen'}], 'source_sha256_checked': bool}.
    """
    files = _source_files(folder, record)
    if not files:
        return "the record lists no source files", {}
    check = check_source(files, (record.get("source") or {}).get("sha256"), _label(record), stream, quiet)
    if check.unreadable:
        return check.reason, {}
    if check.reason is not None:
        return "source changed since plan: {}; {}".format(check.reason, advice), {}
    return None, check.stage_notes()


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
    """Why a record cannot be converted as it stands, or None. Writes nothing; runs nothing."""
    status = record.get("status")
    if status != "planned":
        if status in DONE_STATUSES:
            return ("status is '{}' but the output '{}' is missing or changed since; to convert again, plan it "
                    "again with: vidfunnel plan --replan '{}'".format(status, output_path.name,
                                                                      _source_path(folder, record)))
        return "status is '{}', not 'planned'".format(status)
    action = record.get("action")
    if not isinstance(action, str) or action not in ACTION_WORDS:
        return "action is '{}', not remux or reencode".format(action)
    if "convert" in (record.get("stages") or {}):
        return "the record already has a convert stage; plan it again with: vidfunnel plan --replan '{}'".format(
            _source_path(folder, record))
    return None


CAPABILITY_WORDS = {"encoders": "encoder", "filters": "filter", "bsfs": "bitstream filter", "demuxers": "demuxer",
                    "muxers": "muxer"}


def argv_needs(argv):
    """[(listing, name)] an ffmpeg argv asks of the build, each once, in order. Pure.

    'encoders': the codec after -c:v, -c:a... and -c:s unless it is 'copy';
    'filters': every filter named in a -vf, -filter:v, -af or -filter:a...
    chain; 'bsfs': the bitstream filter of -bsf...; 'demuxers': the input
    format of an -f that comes before the last -i (dvdvideo); 'muxers': the
    output format of an -f after the last -i (mp4, srt, sup, matroska). The
    listing names are those of vidfunnel.ffmpeg.capabilities().
    """
    argv = list(argv)
    inputs = [position for position, item in enumerate(argv) if item == "-i"]
    last_input = inputs[-1] if inputs else -1
    needs = []
    for position, item in enumerate(argv[:-1]):
        value = argv[position + 1]
        found = []
        if item in ("-c:v", "-c:a", "-c:s") or item.startswith("-c:a:"):
            if value != "copy":
                found.append(("encoders", value))
        elif item in ("-vf", "-filter:v", "-af", "-filter:a") or item.startswith("-filter:a:"):
            found += [("filters", part.split("=", 1)[0]) for part in value.split(",")]
        elif item == "-bsf:v" or item == "-bsf":
            found += [("bsfs", part.split("=", 1)[0]) for part in value.split(",")]
        elif item == "-f" and position < last_input:
            found.append(("demuxers", value))
        elif item == "-f":
            found.append(("muxers", value))
        for need in found:
            if need not in needs:
                needs.append(need)
    return needs


def capability_problem(tools, argv, sidecar_argvs=()):
    """None when this ffmpeg's capability listing has everything argv and the sidecar argvs name, else the reason.

    The listing is the one 'check' and the dependency check use
    (vidfunnel.ffmpeg.capabilities, cached per binary); the reason names
    each missing item, once, and the ffmpeg's path. Writes no record.
    """
    try:
        listing = ffmpeg_capabilities(tools)
    except (FfmpegError, ToolNotFound, OSError) as error:
        return "the capabilities of ffmpeg '{}' cannot be listed: {}".format(tools.ffmpeg, error)
    needs = []
    for each in [argv] + list(sidecar_argvs):
        needs += [need for need in argv_needs(each) if need not in needs]
    missing = ["{} '{}'".format(CAPABILITY_WORDS[kind], name) for kind, name in needs
               if name not in (listing.get(kind) or ())]
    if not missing:
        return None
    return "ffmpeg '{}' has no {}, which this record needs (see 'vidfunnel check')".format(
        tools.ffmpeg, " and no ".join(missing))


def _render(tools, record, folder, sidecars=True):
    """(rendered, None) or (None, reason): the record's commands rendered here, with this ffmpeg's identity.

    rendered is plan.render_record's dict plus 'ffmpeg', the identity of
    tools.ffmpeg (one '-version' call per build per process). The values the
    rendering reads are checked first (plan.record_value_problem). `sidecars`
    False renders no sidecar commands (refit). Writes nothing.
    """
    try:
        problem = record_value_problem(record)
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as error:
        return None, "the record's values cannot be checked: {}".format(error)
    if problem is not None:
        return None, problem
    try:
        rendered = render_record(tools, folder, record, sidecars=sidecars)
    except (PlanError, ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as error:
        return None, "the command cannot be rendered from the record: {}".format(error)
    try:
        rendered["ffmpeg"] = current_identity(tools.ffmpeg)
    except (FfmpegError, ToolNotFound, OSError) as error:
        return None, "ffmpeg '{}' does not run here: {}".format(tools.ffmpeg, error)
    return rendered, None


def _conversion(tools, record, folder):
    """(output_path, rendered, reason): what convert_one and --dry-run would do with a record. Writes nothing.

    reason is None when the record is to be converted: its names are plain
    names in its folder, it is not already converted, it is 'planned' with
    a remux or reencode action and no convert stage, its values have the
    forms analyze writes, and this ffmpeg has what the rendered command
    names. output_path is None when a name is refused.
    """
    reason = record_name_problem(record)
    if reason is not None:
        return None, None, reason
    output_path = output_path_for(record, folder)
    reason = skip_reason(record, output_path) or _refusal(record, folder, output_path)
    if reason is not None:
        return output_path, None, reason
    rendered, reason = _render(tools, record, folder)
    if reason is None:
        reason = capability_problem(tools, rendered["argv"], [entry["argv"] for entry in rendered["sidecars"]])
    return output_path, rendered, reason


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


def _remove_if_ours(path, folder):
    """_remove(path) unless the folder's lock is found taken (or cannot be checked): the path may be another run's."""
    if folder is None or lock_problem(folder) is None:
        _remove(path)


def _clear_leftover(temp):
    """Unlink a part file left by an earlier run before a new one is written: (removed, None) or (False, reason).

    Unlinked, never truncated: ffmpeg's '-y' would open the existing file
    with O_TRUNC and keep its inode, and a writer still holding it (an
    encoder orphaned by a vidfunnel killed outright, whose folder another
    machine has since taken over) would go on writing into the new
    encode's file. After the unlink that writer keeps an unnamed file and
    the new encode gets a new one. When the unlink fails for any reason
    but a missing file, the reason names the part file and the item is
    not started.
    """
    try:
        os.unlink(str(temp))
        return True, None
    except FileNotFoundError:
        return False, None
    except OSError as error:
        return False, "cannot remove the part file '{}' left by an earlier run ({}); not started".format(
            Path(temp).name, error.strerror or error)


def _stream_of(progress):
    if progress is None:
        return None, False, False
    return (getattr(progress, "stream", None), bool(getattr(progress, "quiet", False)),
            bool(getattr(progress, "verbose", False)))


def _tagged_progress(progress, tag):
    """A progress namespace like `progress` whose stream starts every line with `tag` ('[convert 3/1054]')."""
    stream, quiet, verbose = _stream_of(progress)
    return types.SimpleNamespace(stream=tagged(stream, tag), quiet=quiet, verbose=verbose)


def write_par2(output_path, redundancy, progress=None):
    """par2create -r N -n 1 -q next to the output; returns the PAR2 files written. Raises OSError on failure.

    Stale PAR2 files of the same name are removed first. An elapsed-time line
    runs while par2 works, since it reports no measurable progress. par2
    inherits the folder locks' descriptors, as vidfunnel.ffmpeg.run's children do.
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
    raise_if_stopped_now("par2")
    with held_lock_fds() as lock_fds:
        process = subprocess.Popen(argv, cwd=str(output_path.parent), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                                   encoding="utf-8", errors="replace", pass_fds=lock_fds)
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


def _write_sidecars(sidecars, record_id, stream, verbose, folder=None):
    """Run each rendered sidecar argv to its own part file and rename; (names written, [error dicts], argvs run).

    With `folder`, LockLost before each part file is cleared and each rename,
    and no part file is removed once the folder's lock is found taken.
    """
    written = []
    errors = []
    started = []
    for entry in sidecars:
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
        if folder is not None:
            ensure_lock(folder)
        _removed, problem = _clear_leftover(temp)
        if problem is not None:
            errors.append({"path": final.name, "error": problem})
            say("  sidecar '{}' failed: {}".format(final.name, problem), stream)
            continue
        started.append(argv)
        try:
            run_tool(argv)
            if folder is not None:
                ensure_lock(folder)
            os.replace(str(temp), str(final))
            written.append(final.name)
        except (FfmpegError, ToolNotFound, OSError) as error:
            errors.append({"path": final.name, "error": str(error),
                           "stderr": _tail(getattr(error, "stderr", None))})
            say("  sidecar '{}' failed: {}".format(final.name, error), stream)
        finally:
            _remove_if_ours(temp, folder)
    return written, errors, started


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
    changed in place and saved after each change of state. Every line it
    prints, the live progress line included, starts with the item's tag,
    '[convert <position>/<total>]' ('[convert]' without them).
    """
    progress = _tagged_progress(progress, item_tag("convert", position, total))
    stream, quiet, verbose = _stream_of(progress)
    verbose = verbose or bool(getattr(options, "verbose", False))
    key = Path(os.path.abspath(os.fspath(source)))
    label = _label(record)
    try:
        return _convert_one(tools, key, record, options, progress, stream, quiet, verbose, label)
    except LockLost as lost:
        # Another run took the folder over: nothing more of this item is renamed, removed or saved there.
        say("skipping '{}': {}".format(label, lost), stream)
        return {"outcome": "skipped", "skipped": str(lost)}


def _convert_one(tools, key, record, options, progress, stream, quiet, verbose, label):
    """convert_one's work; LockLost as soon as the folder's lock is found taken before a write (ensure_lock)."""
    folder = key.parent
    output_path, rendered, reason = _conversion(tools, record, folder)
    if reason is not None:
        say("skipping '{}': {}".format(label, reason), stream)
        return {"outcome": "skipped", "skipped": reason}

    # A source that changed is skipped and its record left as it is; plan re-plans it. A time
    # that differs is noted in this stage, with whether the whole source was read.
    reason, noted = source_change(folder, record, plan_again_advice(folder, record), stream, quiet)
    if reason is not None:
        say("skipping '{}': {}".format(label, reason), stream)
        return {"outcome": "skipped", "skipped": reason}

    if output_path.exists() and not _ours(tools, output_path, record):
        error = "'{}' exists and is not this record's output (no matching vidfunnel tag); not overwritten".format(
            output_path.name)
        stage = {"outcome": "output-collision", "error": error}
        stage.update(noted)
        add_stage(record, "convert", stage)
        add_flag(record, "output-collision")
        set_status(record, "failed")
        save_record(key, record)
        say("refusing '{}': {}".format(label, error), stream)
        return record["stages"]["convert"]

    plan = record["stages"]["plan"]
    action_word = ACTION_WORDS[record["action"]]
    temp = part_path(output_path, record.get("id"))
    argv = list(rendered["argv"])
    argv[-1] = str(temp)
    ran = {"argv": argv, "sidecar_argvs": [], "ffmpeg": rendered["ffmpeg"], "version": vidfunnel.VERSION}
    ran.update(noted)
    say("{} '{}' -> '{}'".format(action_word, label, output_path.name), stream)
    if verbose:
        say(shlex.join(argv), stream)

    ensure_lock(folder)  # a run whose lock was taken must not unlink the new holder's part file
    removed, problem = _clear_leftover(temp)
    if problem is not None:
        say("skipping '{}': {}".format(label, problem), stream)
        return {"outcome": "skipped", "skipped": problem}
    if removed:
        say("  removed a stale '{}' from an earlier run".format(temp.name), stream)
    started = time.monotonic()
    duration = expected_duration(record)
    try:
        with Progress(action_word, total=duration, unit="s", stream=stream, quiet=quiet) as live:
            completed = run_tool(argv, progress=ffmpeg_progress_callback(live, duration))
        # A host that took the folder over meanwhile may have started this record into this part path:
        # nothing is renamed, removed or saved unless the lock is still ours.
        ensure_lock(folder)
        os.replace(str(temp), str(output_path))
    except (FfmpegError, ToolNotFound, OSError) as error:
        ensure_lock(folder)
        _remove(temp)
        stage = {"outcome": "failed", "error": str(error), "returncode": getattr(error, "returncode", None),
                 "stderr": _tail(getattr(error, "stderr", None))}
        stage.update(ran)
        add_stage(record, "convert", stage)
        set_status(record, "failed")
        save_record(key, record)
        say("failed: '{}': {}".format(label, error), stream)
        return record["stages"]["convert"]
    finally:
        _remove_if_ours(temp, folder)
    elapsed = time.monotonic() - started
    # The encode is done: a 'now' stop file no longer cuts this item at its sidecar or PAR2
    # child starts, which would throw the finished encode away; it applies again at verify.
    item_folder = forget_item()

    first_source = _source_files(folder, record)[0][0]
    ensure_lock(folder)
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

    sidecars_written, sidecar_errors, ran["sidecar_argvs"] = _write_sidecars(rendered["sidecars"], record.get("id"),
                                                                             stream, verbose, folder)
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
            ensure_lock(folder)
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
    stage.update(ran)
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
    remember_item(item_folder)
    ensure_lock(folder)
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
    """(argv, filter chain or None): the rendered argv with -crf, the video filter chain and the output replaced. Pure.

    Everything else (inputs, maps, identity tag, movflags) is kept. With
    denoise and no '-vf'/'-filter:v' in the argv, '-vf hqdn3d=...' goes in
    before '-c:v'. Raises ValueError when the argv has no '-crf', or needs a
    filter chain and has no '-c:v' to put it before.
    """
    argv = list(argv)
    try:
        position = argv.index("-crf")
    except ValueError:
        raise ValueError("the rendered command has no -crf")
    if position + 1 >= len(argv) - 1:
        raise ValueError("the rendered command has no value after -crf")
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
            raise ValueError("the rendered command has no -c:v to put the video filter chain before")
        at = argv.index("-c:v")
        argv[at:at] = ["-vf", new_chain]
    argv[-1] = os.fspath(output)
    return argv, new_chain


def _refit_disk_problem(folder, record, output_path, stream=None, quiet=False):
    """(problem or None, output_size, source_size, notes): the files on disk against the record's convert stage.

    The source is checked as convert checks it (source_change). notes holds
    source_mtime_differs and source_sha256_checked (source_change) and
    output_mtime_differs ([{'name', 'recorded', 'seen'}], the output's content
    matching) when a time differs; refit_one writes them into stages.refit.
    """
    convert = (record.get("stages") or {}).get("convert") or {}
    source_size = convert.get("source_size") or (record.get("source") or {}).get("size")
    recorded_size = convert.get("output_size")
    if not isinstance(source_size, int) or source_size <= 0:
        return "the source size is unknown", None, None, {}
    try:
        current = cheap_fingerprint(output_path)
    except OSError:
        return "output '{}' is missing".format(output_path.name), None, source_size, {}
    size = current["size"]
    if recorded_size is not None and size != recorded_size:
        return ("output '{}' is {} bytes, the record says {}; it changed since convert".format(
            output_path.name, size, recorded_size), size, source_size, {})
    recorded = (record.get("output") or {}).get("fingerprint")
    if recorded and not fingerprint_matches(recorded, current):
        return "output '{}' no longer matches its recorded fingerprint".format(output_path.name), size, source_size, {}
    changed, noted = source_change(folder, record, changed_after_convert_advice(folder, record, output_path.name),
                                   stream, quiet)
    if changed is not None:
        return changed, size, source_size, {}
    noted = dict(noted)
    if recorded:
        moved = mtime_difference(recorded, current)
        if moved is not None:
            noted["output_mtime_differs"] = [dict(name=output_path.name, **moved)]
    return None, size, source_size, noted


REFIT_CRF_HIGHEST = 51


def refit_base_crf(record):
    """(crf, field, None) or (None, field, reason): the CRF the existing output was made with. Pure.

    Read from stages.convert.argv, the command convert ran, when the convert
    stage holds one; else from stages.plan.argv, the command a record
    converted before convert recorded its own was run with. Only that number
    is read; neither command is run.
    """
    stages = record.get("stages") or {}
    convert = stages.get("convert")
    if isinstance(convert, dict) and "argv" in convert:
        field, argv = "stages.convert.argv", convert["argv"]
    else:
        plan = stages.get("plan")
        field, argv = "stages.plan.argv", plan.get("argv") if isinstance(plan, dict) else None
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        return None, field, "{} is not a list of strings, so the CRF of the existing output is not known".format(field)
    crf = parse_crf(argv)
    if crf is None or not 0 <= crf <= REFIT_CRF_HIGHEST:
        return None, field, "{} has no '-crf' with a whole number from 0 to {}, so the CRF of the existing output " \
                            "is not known".format(field, REFIT_CRF_HIGHEST)
    return crf, field, None


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


def refit_plan(tools, record, folder, stream=None, quiet=False):
    """(plan dict or None, skip reason or None) for one record; reads the files, writes nothing.

    The command is rendered here (as convert renders it, with the same value
    checks) and its CRF and video filter chain are replaced (refit_argv);
    this ffmpeg's capability listing is checked against the result.
    The plan dict: output_path, part, argv, base_crf, base_crf_from, crf,
    cap, steps, denoise, denoise_reason, filters, ratio, output_size,
    source_size, height, ffmpeg, mtime_notes (_refit_disk_problem).
    """
    reason = refit_selection(record)
    if reason is not None:
        return None, reason
    reason = record_name_problem(record)
    if reason is not None:
        return None, reason
    output_path = output_path_for(record, folder)
    rendered, problem = _render(tools, record, folder, sidecars=False)
    if problem is not None:
        return None, problem
    base_crf, base_field, problem = refit_base_crf(record)
    if problem is not None:
        return None, problem
    problem, output_size, source_size, mtime_notes = _refit_disk_problem(folder, record, output_path, stream, quiet)
    if problem is not None:
        return None, problem
    ratio = float(output_size) / source_size
    if ratio <= 1.0:
        return None, "not larger (output {} bytes, source {} bytes on disk)".format(output_size, source_size)
    argv = rendered["argv"]
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
    problem = capability_problem(tools, new_argv)
    if problem is not None:
        return None, problem
    return {"output_path": output_path, "part": part, "argv": new_argv, "base_crf": base_crf,
            "base_crf_from": base_field, "crf": crf, "cap": cap, "steps": steps, "denoise": denoise,
            "denoise_reason": denoise_reason, "filters": chain, "ratio": ratio, "output_size": output_size,
            "source_size": source_size, "height": height, "ffmpeg": rendered["ffmpeg"],
            "mtime_notes": mtime_notes}, None


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

    Every line it prints, the live progress line included, starts with the
    item's tag, '[refit <position>/<total>]' ('[refit]' without them).
    """
    progress = _tagged_progress(progress, item_tag("refit", position, total))
    stream, quiet, verbose = _stream_of(progress)
    verbose = verbose or bool(getattr(options, "verbose", False))
    key = Path(os.path.abspath(os.fspath(source)))
    folder = key.parent
    label = _label(record)
    plan, reason = refit_plan(tools, record, folder, stream, quiet)
    if plan is None:
        say("skipping '{}': {}".format(label, reason), stream)
        return "skipped", reason
    say("'{}': {}".format(label, _refit_words(plan)), stream)
    say("  {}".format(plan["denoise_reason"]), stream)
    if verbose:
        say(shlex.join(plan["argv"]), stream)
    temp = plan["part"]
    output_path = plan["output_path"]
    try:
        ensure_lock(folder)  # a run whose lock was taken must not unlink the new holder's part file
    except LockLost as lost:
        say("skipping '{}': {}".format(label, lost), stream)
        return "skipped", str(lost)
    removed, problem = _clear_leftover(temp)  # before the record is touched: a skip leaves it as it was
    if problem is not None:
        say("skipping '{}': {}".format(label, problem), stream)
        return "skipped", problem
    if removed:
        say("  removed a stale '{}' from an earlier run".format(temp.name), stream)

    stages = record.setdefault("stages", {})
    if "refit" in stages:
        stages.setdefault("refit_history", []).append(stages.pop("refit"))
    attempt = {"crf": plan["crf"], "base_crf": plan["base_crf"], "base_crf_from": plan["base_crf_from"],
               "cap": plan["cap"], "steps": plan["steps"],
               "denoise": plan["denoise"], "denoise_reason": plan["denoise_reason"], "filters": plan["filters"],
               "previous_output_size": plan["output_size"], "previous_ratio": round(plan["ratio"], 4),
               "source_size": plan["source_size"], "output_size": None, "ratio": None, "elapsed_s": None,
               "argv": plan["argv"], "ffmpeg": plan["ffmpeg"], "version": vidfunnel.VERSION}
    started = time.monotonic()
    duration = expected_duration(record)
    try:
        return _refit_attempt(tools, key, record, options, progress, plan, attempt, label, started, duration)
    except LockLost as lost:
        # Another run took the folder over: nothing more of this item is renamed, removed or saved there.
        say("skipping '{}': {}".format(label, lost), stream)
        return "skipped", str(lost)
    finally:
        # Gone after a replace or a removal; this catches an exception between the encode and either.
        _remove_if_ours(temp, folder)


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
        ensure_lock(folder)
        _remove(temp)
        attempt["elapsed_s"] = round(time.monotonic() - started, 3)
        add_stage(record, "refit", dict({"attempt": attempt, "outcome": "failed", "error": str(error),
                                         "stderr": _tail(getattr(error, "stderr", None))}, **plan["mtime_notes"]))
        save_record(key, record)
        say("failed: refit of '{}': {}; the existing output is kept".format(label, error), stream)
        return "failed", str(error)
    # A host that took the folder over meanwhile may have started this record into this part path:
    # nothing is done with the result unless the lock is still ours.
    ensure_lock(folder)
    attempt["elapsed_s"] = round(time.monotonic() - started, 3)
    attempt["ffmpeg_stderr"] = _tail(completed.stderr)
    # The encode is done: a 'now' stop file no longer cuts this item at the checks' ffprobe or
    # the PAR2 child start, which would throw the finished refit away; it applies again at verify.
    item_folder = forget_item()

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
        ensure_lock(folder)
        _remove(temp)
        add_stage(record, "refit", dict({"attempt": attempt, "outcome": "kept-existing", "reason": problem},
                                        **plan["mtime_notes"]))
        add_flag(record, FIT_CAPPED_FLAG)
        save_record(key, record)
        say("  refit '{}': {}; {}; kept the existing output, flagged '{}'".format(
            label, result_words, problem, FIT_CAPPED_FLAG), stream)
        return "kept", problem

    ensure_lock(folder)
    try:
        os.replace(str(temp), str(output_path))
    except OSError as error:
        ensure_lock(folder)
        _remove(temp)
        add_stage(record, "refit", dict({"attempt": attempt, "outcome": "failed",
                                         "error": "could not replace '{}': {}".format(output_path.name, error)},
                                        **plan["mtime_notes"]))
        save_record(key, record)
        say("failed: refit of '{}': could not replace '{}': {}".format(label, output_path.name, error), stream)
        return "failed", str(error)

    # The file on disk is now the refit. Everything that describes it goes into the
    # record and is saved before the slow PAR2 step, so an interrupt there leaves a
    # record that says what happened (a convert PAR2 set is then stale, see below).
    first_source = _source_files(folder, record)[0][0]
    ensure_lock(folder)
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
    add_stage(record, "refit", dict({"attempt": attempt, "outcome": "replaced"}, **plan["mtime_notes"]))
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
        ensure_lock(folder)
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
    remember_item(item_folder)
    ensure_lock(folder)
    _verify(tools, key, record, options, progress, stream)
    if record.get("status") == "failed":
        return "failed", "verify failed after the refit"
    return "refitted", None


def _refit_dry_run(tools, entries, stream):
    total = len(entries)
    shown = 0
    for position, (key, folder, record) in enumerate(entries, 1):
        plan, reason = refit_plan(tools, record, folder, stream)
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
    refused = []
    say("scanning {} path(s) for records to refit".format(len(paths)), out)
    entries = _collect(paths, out, problems, refused)
    summary.add("failed", len(problems))
    if getattr(options, "dry_run", False):
        shown = _refit_dry_run(tools, entries, out)
        say("convert --refit: dry run, {} record(s) would be refitted; nothing written".format(shown), out)
        return summary

    def skipped(reason):
        summary.add("skipped")
        short = reason if reason in ("not converted", "not a re-encode", "already capped", "already refitted",
                                     "not larger", "folder locked") else "other (reason printed above)"
        summary.skip_reasons[short] = summary.skip_reasons.get(short, 0) + 1

    for reason in refused:
        skipped(reason)
    selected = []
    for key, folder, record in entries:
        reason = refit_selection(record)
        if reason is None:
            selected.append((key, folder))
        else:
            skipped(reason)  # no lock: nothing is written for a record that is not selected

    # No lock is taken here: each folder's lock is taken when its first item starts and released
    # after its last one (vidfunnel.turns), so the folders not being worked on stay free for other hosts.
    runnable = _folder_order(selected)
    total = len(runnable)
    turns = FolderTurns(_item_counts(runnable), lambda message: say(message, out), lock_errors=summary.lock_errors)
    try:
        def work(position, key):
            tag = item_tag("refit", position, total)
            try:
                record = load_record(key)
            except RecordError as error:
                say("{} failed: {}".format(tag, error), out)
                return "failed", None
            if record is None:
                say("{} failed: the record of '{}' disappeared".format(tag, key.name), out)
                return "failed", None
            return refit_one(tools, key, record, options, shared, position, total)

        def item(position, key, folder):
            """(outcome, reason), or None when the run drained before the folder was entered (not started)."""
            try:
                turn = turns.enter(folder)
                if turn == DRAINING:
                    return None
                if turn != HELD:
                    return "skipped", "folder locked"
                return work(position, key)
            finally:
                turns.leave(folder)

        results = []
        if jobs == 1:
            for position, (key, folder) in enumerate(runnable, 1):
                if drain_before_next(len(runnable) - position + 1, folder):
                    break
                result = item(position, key, folder)
                if result is None:
                    drain_before_next(len(runnable) - position + 1)  # draining: counts this item and the rest left
                    break
                results.append(result)
                drain_after_item()
        else:
            results = [None] * len(runnable)

            def refit_done(index, value):
                if value is None:
                    not_started()
                results[index] = value

            run_pool(jobs, [lambda position=position, key=key, folder=folder: item(position, key, folder)
                            for position, (key, folder) in enumerate(runnable, 1)], refit_done,
                     folders=[folder for _key, folder in runnable])
        for outcome, reason in [result for result in results if result is not None]:  # None: not started (drain)
            if outcome == "skipped":
                skipped(reason)
            else:
                summary.add(outcome)
    finally:
        turns.close()
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

    def fileno(self):
        """The target's descriptor, for vidfunnel.progress.discard_output."""
        return self.target.fileno()


def _collect(paths, stream, problems, refused):
    """[(key, folder, record)] for every record under the PATHs, each once.

    A record whose source section cannot key it (plan.record_key_problem: a
    source name with a directory part would name another folder's record)
    is left out, its reason printed and appended to `refused`.
    """
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
            problem = record_key_problem(record)
            if problem is not None:
                say("skipping a record in '{}': {}".format(folder, problem), stream)
                refused.append(problem)
                continue
            key = record_key(folder, record)
            if str(key) in seen:
                continue
            seen.add(str(key))
            found.append((key, folder, record))
    return found


def _dry_run(tools, entries, options, stream):
    """Print the commands convert would run, rendered here, and each skip reason; writes nothing."""
    total = len(entries)
    for position, (key, folder, record) in enumerate(entries, 1):
        output_path, rendered, reason = _conversion(tools, record, folder)
        if reason is not None:
            say("[convert {}/{}] would skip '{}': {}".format(position, total, _label(record), reason), stream)
            continue
        plan = record["stages"]["plan"]
        argv = list(rendered["argv"])
        argv[-1] = str(part_path(output_path, record.get("id")))
        say("[convert {}/{}] would {} '{}' -> '{}'".format(position, total, ACTION_WORDS[record["action"]],
                                                          _label(record), output_path.name), stream)
        say(shlex.join(argv), stream)
        say("  then rename to '{}'".format(output_path), stream)
        for entry in rendered["sidecars"]:
            say(shlex.join(entry["argv"]), stream)
        redundancy = getattr(options, "par2", None)
        if redundancy is None:
            redundancy = plan.get("par2")
        if redundancy:
            say("  then par2 create -r{} -n1 for '{}'".format(redundancy, output_path.name), stream)


def _folder_order(items):
    """[(key, folder)] with each folder's items together, folders in order of first appearance, items in order."""
    by_folder = {}
    for key, folder in items:
        by_folder.setdefault(str(folder), []).append((key, folder))
    return [item for members in by_folder.values() for item in members]


def _item_counts(items):
    """{folder name: number of items} of [(key, folder)]."""
    counts = {}
    for _key, folder in items:
        counts[str(folder)] = counts.get(str(folder), 0) + 1
    return counts


def _finished_record(key, folder):
    """The record when convert would only report it as already converted (its output still matches), else None.

    The same first checks as _conversion (plain names, then skip_reason),
    read without a lock and writing nothing; a record that cannot be read
    or judged here is None, and the item then runs under the folder's lock.
    """
    try:
        record = load_record(key)
        if record is None or record_name_problem(record) is not None:
            return None
        if skip_reason(record, output_path_for(record, folder)) == "already converted":
            return record
    except (RecordError, ValueError, TypeError, AttributeError, KeyError):
        return None
    return None


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
    refused = []
    say("scanning {} path(s) for records".format(len(paths)), out)
    entries = _collect(paths, out, problems, refused)
    summary.add("failed", len(problems))
    summary.add("skipped", len(refused))
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
        _dry_run(tools, wanted, options, out)
        say("convert: dry run, {} record(s) shown; nothing written".format(len(wanted)), out)
        return summary

    # No lock is taken here: each folder's lock is taken when its first item that needs one starts and
    # released after its last item (vidfunnel.turns). The output-name check runs then, under that lock.
    runnable = _folder_order([(key, folder) for key, folder, _record in wanted])
    total = len(runnable)
    collided = set()
    members_of = {}
    for key, folder in runnable:
        members_of.setdefault(str(folder), []).append((key, folder))

    def check_outputs(folder):
        members = members_of.get(str(folder), [])
        kept, _count = _shared_outputs(members, out)
        kept_keys = set(str(key) for key, _member_folder in kept)
        collided.update(str(key) for key, _member_folder in members if str(key) not in kept_keys)

    turns = FolderTurns(_item_counts(runnable), lambda message: say(message, out), on_enter=check_outputs,
                        lock_errors=summary.lock_errors)
    try:
        def work(position, key):
            tag = item_tag("convert", position, total)
            try:
                record = load_record(key)
            except RecordError as error:
                say("{} failed: {}".format(tag, error), out)
                return "failed"
            if record is None:
                say("{} failed: the record of '{}' disappeared".format(tag, key.name), out)
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

        def item(position, key, folder):
            """The item's outcome, or None when the run drained before the folder was entered (not started)."""
            try:
                finished = _finished_record(key, folder)
                if finished is not None:
                    # Already converted, output unchanged: reported without a lock, as nothing is written.
                    say("skipping '{}': already converted".format(_label(finished)),
                        tagged(out, item_tag("convert", position, total)))
                    return "skipped"
                turn = turns.enter(folder)
                if turn == DRAINING:
                    return None
                if turn != HELD:
                    return "skipped"
                if str(key) in collided:
                    return "collision"
                return work(position, key)
            finally:
                turns.leave(folder)

        def done(_index, outcome):
            if outcome is None:
                not_started()
            else:
                summary.add(outcome)

        if jobs == 1:
            for position, (key, folder) in enumerate(runnable, 1):
                if drain_before_next(len(runnable) - position + 1, folder):
                    break
                outcome = item(position, key, folder)
                if outcome is None:
                    drain_before_next(len(runnable) - position + 1)  # draining: counts this item and the rest left
                    break
                summary.add(outcome)
                drain_after_item()
        else:
            run_pool(jobs, [lambda position=position, key=key, folder=folder: item(position, key, folder)
                            for position, (key, folder) in enumerate(runnable, 1)],
                     done, folders=[folder for _key, folder in runnable])
    finally:
        turns.close()
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
