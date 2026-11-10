"""verify: check a converted output against its source and score it (HANDOFF.md sections 4.3, 5, 6.4 and 6.7).

For each record with status 'converted' (or 'verified', to verify again) the
output is looked up beside the record's source under output.interim_name,
else output.name, and checked:

- a full null decode of every video and audio stream, its message classes
  judged by analyze.decide_decode (10 points per class, benign classes free);
- an ffprobe of the output: duration, stream counts, codec, profile, level and
  pixel format of the video, and the 'vidfunnel' identity tag read back,
  whose 'id' must equal the record id and whose 'src' must equal the first 16
  hex digits of source.sha256; a missing or different tag is
  'identity-mismatch' (100 points: the file is not the one this record
  describes);
- the output's exact video frame count (ffprobe -count_packets) against
  expected_frames(): the source frame count times the cadence fps_multiplier
  (2.0 for field-rate deinterlacing), or duration x 24000/1001 for an inverse
  telecine; outside 2 % plus 2 frames either way costs 30 points;
- the video durations of source and output (source: analyze's packet
  measurement, else the probed stream, else the container; output: the
  video stream): more than 0.1 s plus one output frame apart costs 20 points;
- the stream counts against expected_streams(): a missing video, audio or
  text-subtitle stream costs 50 points (HANDOFF 5) and is flagged
  'stream-count-mismatch' so the report shows it;
- with --ssim-check, sampled SSIM on luma against the source put through the
  same video filters as the encode (below the threshold costs 25 points);
- the output's SHA-256, size and cheap fingerprint, taken before the checks
  above read the file, and its size relative to the source (an output larger
  than the source is noted; retire refuses it without --allow-larger). The
  cheap fingerprint is taken again after the last check; when it differs the
  stage records 'output_changed_during_verify': true, the record is 'failed'
  and carries no score, since the hash and the checks may describe different
  bytes.

`run` collects the records first and then, inside each folder's lock, reads
each record again from disk and verifies that copy; a record whose status is
no longer 'converted' or 'verified' (a retire finished in between) or that is
gone is skipped, so verify never overwrites a later stage. Before each folder
and each record it asks vidfunnel.interrupt.drain_before_next, which also
looks for a stop file above the folder: on a drain (SIGUSR1, 'stop-all') no
further record starts, and a 'now' stop file stops the run as Ctrl+C would.

The score is 100 minus the penalties the record carried before any verify
(the analysis, and plan's own VOB/DVD penalties) minus this run's
post-conversion penalties, floored at 0. Penalties added by verify are marked
{'stage': 'verify'} in record['penalties'], so verifying again replaces them
instead of adding to them; the previous stages.verify is archived in
stages.verify_history. Status 'verified' when the checks ran, penalties or not
(the band of the score says what it means); 'failed' when the output is
missing, cannot be probed, does not decode at all (non-zero exit and no
decoded frame), or changed while it was verified.

Every name a record gives (source.name, the VOB members, output.name and
interim_name, the sidecar names) is checked with plan.record_name_problem,
as convert checks it, before anything the record names is read: `run`
leaves a record whose names are not plain names in its folder out when it
collects the records, so it is never keyed, loaded or saved, and counts it
'skipped'; verify_record raises RecordRefused (a ValueError) for one before
it touches a file or the record. With --ssim-check, for a re-encode, the
values that go into the comparison's filter graph are checked as convert
checks them (plan.record_value_problem), in verify_record before any file
is read, and a record that fails is refused the same way.
"""

VERSION = "20261109.3"

import json
import os
import re
import sys
import types
from fractions import Fraction
from pathlib import Path

from vidfunnel.analyze import DEFAULT_SAMPLES, WINDOW_SECONDS, decide_decode, sample_windows
from vidfunnel.ffmpeg import FfmpegError, null_decode
from vidfunnel.ffmpeg import run as run_tool
from vidfunnel.interrupt import drain_after_item, drain_before_next
from vidfunnel.plan import HASH_PROGRESS_BYTES, IDENTITY_KEY, record_name_problem, record_value_problem, video_filters
from vidfunnel.probe import count_frames, probe_file
from vidfunnel.progress import Progress, StageSummary, ffmpeg_progress_callback, format_bytes, item_tag, say, tagged
from vidfunnel.record import (
    RECORD_DIR,
    FolderLock,
    LockHeld,
    RecordError,
    add_flag,
    add_stage,
    band,
    cheap_fingerprint,
    fingerprint_identical,
    iter_records,
    load_record,
    save_record,
    set_status,
    sha256_file,
)

DURATION_TOLERANCE_S = 0.1
FRAME_TOLERANCE = 0.02          # 2 % either way, after the cadence adjustment ...
FRAME_SLACK = 2                 # ... plus 2 frames absolute
DEFAULT_SSIM_THRESHOLD = 0.97
TELECINE_FPS = Fraction(24000, 1001)

PENALTY_DECODE_CLASS = 10       # per class, through analyze.decide_decode
PENALTY_DURATION = 20
PENALTY_FRAMES = 30
PENALTY_SSIM = 25
PENALTY_IDENTITY = 100
PENALTY_STREAMS = 50           # a stream the source had is missing from the output: hold band

VERIFY_STAGE_MARK = "verify"
VERIFIABLE_STATUSES = ("converted", "verified")
OUTCOMES = ("ok", "review", "hold", "failed", "skipped")

_SSIM_LUMA = re.compile(r"SSIM Y:([0-9.]+)")


class RecordRefused(ValueError):
    """A record verify does not work on: a name in it is not a plain file name in its folder (the field named)."""


# --- small helpers ---------------------------------------------------------------------


def _progress_options(progress):
    if progress is None:
        return None, False
    return getattr(progress, "stream", None), bool(getattr(progress, "quiet", False))


def _fraction(text):
    if not text:
        return None
    try:
        value = Fraction(str(text))
    except (ValueError, ZeroDivisionError):
        return None
    return value if value > 0 else None


def _stages(record):
    return record.get("stages") or {}


def _source_probe(record):
    return _stages(record).get("probe") or {}


def _analysis(record):
    return _stages(record).get("analyze") or {}


def _source_duration(record):
    """Duration for frame arithmetic: the video stream's, else the container's."""
    probe = _source_probe(record)
    video = probe.get("video") or {}
    return video.get("duration") or (probe.get("format") or {}).get("duration")


def _cadence(record):
    return (_analysis(record).get("cadence") or {}).get("result") or {}


def output_path_of(folder, record):
    """Where the output of a record is on disk: the interim name when set, else the final name; None if unnamed."""
    output = record.get("output") or {}
    name = output.get("interim_name") or output.get("name")
    if not name:
        return None
    return Path(folder) / name


def record_key(folder, record):
    """The path record.save_record/load_record key a record by (plan's naming).

    A file or VOB group: '<folder>/<source.name>'. A DVD title:
    '<folder>/<DVD folder name>_titleNN'.
    """
    source = record.get("source") or {}
    if source.get("kind") == "dvd-title" and source.get("dvd_title") is not None:
        return Path(folder) / "{}_title{:02d}".format(source.get("name"), int(source["dvd_title"]))
    return Path(folder) / source.get("name")


def source_inputs(folder, record):
    """The source file(s) beside the record: the VOB members in order, the file itself, or the DVD folder."""
    source = record.get("source") or {}
    members = source.get("members")
    # As plan.record_inputs: the members are the inputs of a VOB group only, the kind whose names are checked.
    if members and source.get("kind") == "vob-group":
        return [Path(folder) / member.get("name") for member in members]
    return [Path(folder) / source.get("name")]


# --- expectations ----------------------------------------------------------------------


def expected_frames(record, measured_source_frames=None):
    """The output frame count the record leads to expect, or None when it cannot be known.

    Telecine (cadence kind 'telecined'): source duration x 24000/1001
    (fieldmatch,decimate keeps 4 frames of 5). Otherwise the source frame
    count times the cadence fps_multiplier (2.0 for field-rate deinterlacing,
    1.0 otherwise). The source frame count is, in order: measured_source_frames
    (an exact count the caller made with probe.count_frames), the packet count
    analyze measured (stages.analyze.sync.durations.video.packets), the probed
    video nb_frames (container headers can be off by a frame: an AVI with a
    skipped frame says 126 while 125 packets exist), else duration x fps. The last is the fallback for a VFR
    source whose container gives no frame count and which is no longer there to
    count: for VFR, fps is ffprobe's r_frame_rate, so the estimate may be off,
    and a frame-count penalty on such a record deserves a look before it is
    trusted. Duration is the video stream's, else the container's.
    """
    cadence = _cadence(record)
    duration = _source_duration(record)
    if cadence.get("kind") == "telecined":
        rate = _fraction(cadence.get("output_fps")) or TELECINE_FPS
        if duration is None:
            return None
        return int(round(float(duration) * float(rate)))
    video = _source_probe(record).get("video") or {}
    analysed_packets = (((_analysis(record).get("sync") or {}).get("durations") or {}).get("video") or {}).get("packets")
    if measured_source_frames is not None:
        frames = float(measured_source_frames)
    elif analysed_packets:
        frames = float(analysed_packets)
    elif video.get("nb_frames"):
        frames = float(video["nb_frames"])
    elif duration is not None and video.get("fps"):
        frames = float(duration) * float(video["fps"])
    else:
        return None
    multiplier = float(cadence.get("fps_multiplier") or 1.0)
    return int(round(frames * multiplier))


def expected_streams(record):
    """{'video': 1, 'audio': n, 'subtitle': n_mov_text}: what plan's argv maps into the MP4.

    Every audio stream is mapped; only subtitle streams whose action is
    'mov_text' go into the MP4 (bitmap subtitles go to sidecars).
    """
    analysis = _analysis(record)
    audio = analysis.get("audio")
    if audio is None:
        audio_count = len(_source_probe(record).get("audio") or [])
    else:
        audio_count = len(audio)
    subtitles = (analysis.get("subtitles") or {}).get("result") or []
    text_count = sum(1 for entry in subtitles if entry.get("action") == "mov_text")
    return {"video": 1, "audio": audio_count, "subtitle": text_count}


# --- comparisons -----------------------------------------------------------------------


def compare_durations(source_duration, output_duration, tolerance=DURATION_TOLERANCE_S):
    """{'source', 'output', 'delta', 'tolerance', 'ok'}: ok when both are known and differ by at most tolerance."""
    if source_duration is None or output_duration is None:
        return {"source": source_duration, "output": output_duration, "delta": None, "tolerance": tolerance,
                "ok": False}
    delta = float(output_duration) - float(source_duration)
    return {"source": float(source_duration), "output": float(output_duration), "delta": round(delta, 6),
            "tolerance": tolerance, "ok": abs(delta) <= tolerance + 1e-9}


def source_video_duration(record):
    """(seconds, kind) of the source's video for the duration check; (None, None) when nothing is known.

    In order: the video duration analyze measured from the packet list
    (stages.analyze.sync.durations.video.duration, kind 'packets'); the
    probed video stream's duration when ffprobe gave it for the stream
    (duration_source 'stream', kind 'stream'); the container duration (kind
    'format'). Container durations include AVI padding and audio that runs
    past the video, which are not conversion defects.
    """
    measured = ((((_analysis(record).get("sync") or {}).get("durations") or {}).get("video")) or {}).get("duration")
    if measured is not None:
        return float(measured), "packets"
    probe = _source_probe(record)
    video = probe.get("video") or {}
    if video.get("duration") is not None and video.get("duration_source") == "stream":
        return float(video["duration"]), "stream"
    duration = (probe.get("format") or {}).get("duration")
    if duration is not None:
        return float(duration), "format"
    return None, None


def output_video_duration(probe):
    """(seconds, kind) of the output's video stream ('stream'), else the container's ('format')."""
    video = probe.get("video") or {}
    if video.get("duration") is not None:
        return float(video["duration"]), "stream"
    duration = (probe.get("format") or {}).get("duration")
    if duration is not None:
        return float(duration), "format"
    return None, None


def duration_tolerance(output_fps):
    """DURATION_TOLERANCE_S plus one output frame interval, so a one-frame difference never costs points."""
    if output_fps:
        return DURATION_TOLERANCE_S + 1.0 / float(output_fps)
    return DURATION_TOLERANCE_S


def frame_range(expected):
    """(low, high) accepted around an expected frame count: 2 % either way plus 2 frames."""
    low = int(expected * (1.0 - FRAME_TOLERANCE)) - FRAME_SLACK
    high = int(-(-expected * (1.0 + FRAME_TOLERANCE) // 1)) + FRAME_SLACK
    return max(0, low), high


def compare_frames(expected, actual):
    """{'expected', 'actual', 'low', 'high', 'delta', 'ok'}; ok False when either is unknown."""
    if expected is None or actual is None:
        return {"expected": expected, "actual": actual, "low": None, "high": None, "delta": None, "ok": False}
    low, high = frame_range(expected)
    return {"expected": expected, "actual": actual, "low": low, "high": high, "delta": actual - expected,
            "ok": low <= actual <= high}


def actual_streams(probe):
    """{'video', 'audio', 'subtitle'} counts of a probe (cover-art pictures are not video)."""
    video = 0
    for stream in probe.get("streams") or []:
        if stream.get("type") == "video" and not (stream.get("disposition") or {}).get("attached_pic"):
            video += 1
    return {"video": video, "audio": len(probe.get("audio") or []), "subtitle": len(probe.get("subtitles") or [])}


def compare_streams(expected, actual_probe):
    """{'expected', 'actual', 'ok'}: per-type counts of the output probe against expected_streams()."""
    actual = actual_streams(actual_probe)
    ok = all(actual.get(kind) == expected.get(kind) for kind in ("video", "audio", "subtitle"))
    return {"expected": dict(expected), "actual": actual, "ok": ok}


def read_tag(probe):
    """The parsed 'vidfunnel' format tag of a probe, or None (absent, not JSON, not an object)."""
    for name, value in ((probe.get("format") or {}).get("tags") or {}).items():
        if name.lower() == IDENTITY_KEY:
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError):
                return None
            return parsed if isinstance(parsed, dict) else None
    return None


def check_identity(record, tag):
    """{'tag', 'ok', 'reason'}: the tag names this record's id and the first 16 hex of its source SHA-256."""
    expected_src = ((record.get("source") or {}).get("sha256") or "")[:16]
    if tag is None:
        return {"tag": None, "ok": False, "reason": "the output carries no readable '{}' tag".format(IDENTITY_KEY)}
    problems = []
    if tag.get("tool") != IDENTITY_KEY:
        problems.append("tool is {!r}".format(tag.get("tool")))
    if tag.get("id") != record.get("id"):
        problems.append("id {!r} is not the record's {!r}".format(tag.get("id"), record.get("id")))
    if not expected_src or tag.get("src") != expected_src:
        problems.append("src {!r} is not the source's {!r}".format(tag.get("src"), expected_src))
    if problems:
        return {"tag": tag, "ok": False, "reason": "; ".join(problems)}
    return {"tag": tag, "ok": True, "reason": "id and src match the record"}


# --- measurements that run ffmpeg ------------------------------------------------------


def decode_output(tools, output_path, progress=None, total_seconds=None):
    """Full null decode of the output; the decide_decode classes plus the decoded frame count.

    `progress` is a vidfunnel.progress.Progress (or None). Returns
    {'returncode', 'classes', 'ignored', 'reason', 'penalties', 'flags',
    'frames_decoded'}; a non-zero returncode is a failure the caller judges
    (with frames_decoded 0 the output does not decode at all).
    """
    last = {"frame": 0}
    forward = ffmpeg_progress_callback(progress, total_seconds) if progress is not None else None

    def on_block(block):
        if block.get("frame") is not None:
            last["frame"] = block["frame"]
        if forward is not None:
            forward(block)

    result = null_decode(tools, output_path, on_block)
    decision = decide_decode(result)
    found = decision["result"]
    return {"returncode": found["returncode"], "classes": found["classes"], "ignored": found["ignored"],
            "reason": decision["reason"], "penalties": decision["penalties"], "flags": decision["flags"],
            "frames_decoded": last["frame"]}


def _ssim_argv(tools, source_inputs, output_path, window, source_filter, rate):
    start, length = window
    timing = []
    if start:
        timing += ["-ss", "{:.3f}".format(start)]
    if length is not None:
        timing += ["-t", "{:.3f}".format(length)]
    argv = [tools.ffmpeg, "-hide_banner", "-nostdin", "-nostats"]
    if len(source_inputs) == 1:
        argv += timing + ["-i", os.path.abspath(os.fspath(source_inputs[0]))]
    else:
        argv += timing + ["-i", "concat:" + "|".join(os.path.abspath(os.fspath(path)) for path in source_inputs)]
    argv += timing + ["-i", os.path.abspath(os.fspath(output_path))]
    reference = "[0:v:0]" + (source_filter + "," if source_filter else "")
    common = "setpts=PTS-STARTPTS,format=yuv420p" + (",fps={}".format(rate) if rate else "")
    graph = "{}{}[reference];[1:v:0]{}[converted];[converted][reference]ssim".format(reference, common, common)
    argv += ["-lavfi", graph, "-an", "-sn", "-f", "null", "-"]
    return argv


def sampled_ssim(tools, source_inputs, output_path, windows, progress=None, source_filter=None, rate=None):
    """Mean luma SSIM over the windows, or None when no window produced a value (or there are no windows).

    For each (start, length) window (length None: to the end, as
    analyze.sample_windows gives for short files) both inputs are cut with
    -ss/-t; the
    source goes through `source_filter` (the encode's -vf chain, so a
    deinterlaced or inverse-telecined output is compared with its own
    reference), both are reset to start at 0, converted to yuv420p and, with
    `rate`, to that frame rate, and the ssim filter's 'SSIM Y:' value is read
    from stderr. A window whose sizes differ (the ssim filter refuses it) or
    that fails gives no value. `progress` (a
    Progress with unit 'items') gets the window counter.
    """
    values = []
    for number, window in enumerate(windows or [], 1):
        if progress is not None:
            progress.update(number - 1, extra="ssim window {}/{}".format(number, len(windows)))
        completed = run_tool(_ssim_argv(tools, source_inputs, output_path, window, source_filter, rate), check=False)
        found = _SSIM_LUMA.findall(completed.stderr or "")
        if completed.returncode == 0 and found:
            values.append(float(found[-1]))
    if progress is not None and windows:
        progress.update(len(windows), extra="ssim done")
    if not values:
        return None
    return sum(values) / len(values)


# --- scoring ---------------------------------------------------------------------------


def base_penalties(record):
    """The penalties the record carried before any verify run (analysis, plan): those not marked by verify."""
    return [entry for entry in record.get("penalties") or [] if entry.get("stage") != VERIFY_STAGE_MARK]


def post_conversion_penalties(checks):
    """[{'code', 'points', 'reason'}] for the checks of one verify run (HANDOFF 5)."""
    entries = []
    decode = checks.get("decode") or {}
    for entry in decode.get("penalties") or []:
        entries.append({"code": entry["code"], "points": entry["points"], "reason": "output: " + entry["reason"]})
    duration = checks.get("duration")
    if duration is not None and not duration.get("ok"):
        entries.append({"code": "duration-mismatch", "points": PENALTY_DURATION,
                        "reason": "output video duration {} differs from the source's {} by more than {} s".format(
                            duration.get("output"), duration.get("source"),
                            round(duration.get("tolerance") or DURATION_TOLERANCE_S, 4))})
    frames = checks.get("frames")
    if frames is not None and not frames.get("ok"):
        entries.append({"code": "frame-count", "points": PENALTY_FRAMES,
                        "reason": "output has {} frames, expected {} (accepted {} to {})".format(
                            frames.get("actual"), frames.get("expected"), frames.get("low"), frames.get("high"))})
    ssim = checks.get("ssim")
    if ssim is not None and ssim.get("requested") and not ssim.get("ok"):
        entries.append({"code": "ssim-low", "points": PENALTY_SSIM,
                        "reason": "sampled luma SSIM {} below {}".format(ssim.get("value"), ssim.get("threshold"))
                        if ssim.get("value") is not None else "sampled SSIM could not be measured"})
    identity = checks.get("identity")
    if identity is not None and not identity.get("ok"):
        entries.append({"code": "identity-mismatch", "points": PENALTY_IDENTITY, "reason": identity.get("reason")})
    streams = checks.get("streams")
    if streams is not None and not streams.get("ok"):
        entries.append({"code": "stream-count", "points": PENALTY_STREAMS,
                        "reason": "output streams {} differ from the expected {}".format(
                            streams.get("actual"), streams.get("expected"))})
    return entries


def score_record(record, checks):
    """(score, penalties_added): 100 minus the pre-verify penalties minus this run's, floored at 0.

    The pre-verify penalties are record['penalties'] without the entries an
    earlier verify marked, so a second run never counts the first run's
    points again. A quarantined record scores 0. penalties_added are this
    run's entries (not yet in the record).
    """
    added = post_conversion_penalties(checks)
    if record.get("status") == "quarantined":
        return 0, added
    total = sum(int(entry.get("points") or 0) for entry in base_penalties(record))
    total += sum(entry["points"] for entry in added)
    return max(0, 100 - total), added


# --- one record ------------------------------------------------------------------------


def _option(options, name, default):
    value = getattr(options, name, None) if options is not None else None
    return default if value is None else value


def _archive_previous(record):
    """Move an earlier verify stage into stages.verify_history and undo its penalties and flags."""
    stages = record.setdefault("stages", {})
    previous = stages.pop("verify", None)
    if previous is None:
        return
    stages.setdefault("verify_history", []).append(previous)
    record["penalties"] = base_penalties(record)
    added_flags = previous.get("flags_added") or []
    record["flags"] = [flag for flag in record.get("flags") or [] if flag not in added_flags]


def _fail(record, key, stage, message, stream):
    stage["error"] = message
    add_stage(record, "verify", stage)
    set_status(record, "failed")
    save_record(key, record)
    say("  verify failed: {}".format(message), stream)
    return record


def _measure_source_frames(tools, folder, record, stream, progress=None):
    """(count, how) for the source video frames: nb_frames, an exact count of the files, or (None, why).

    `progress` (the item's, with its tagged stream) gets the count's start line.
    """
    video = _source_probe(record).get("video") or {}
    if video.get("nb_frames"):
        return None, "nb_frames"
    if (record.get("source") or {}).get("kind") == "dvd-title" or video.get("index") is None:
        return None, "estimate"
    paths = source_inputs(folder, record)
    if not all(path.is_file() for path in paths):
        return None, "estimate (source not present)"
    try:
        total = 0
        for path in paths:
            total += count_frames(tools, path, video["index"], progress)
        return total, "counted"
    except FfmpegError as error:
        say("  could not count source frames: {}".format(error), stream)
        return None, "estimate (count failed)"


def verify_record(tools, source, record, options, progress=None):
    """Verify the output of one record and return the record, saved (see the module docstring).

    `source` is the path the record is keyed by (record_key()); the output is
    looked up in its folder. `options` may carry ssim_check, ssim_threshold
    and samples. `progress` is None or an object with 'stream' and 'quiet'
    (the CLI args). Raises ValueError for a record whose status is not
    'converted' or 'verified', and RecordRefused (a ValueError) for one whose
    names are not plain names in its folder (plan.record_name_problem) or,
    when the SSIM pass will build a filter graph from the analysis (ssim_check
    on a re-encode), whose values are not of the forms analyze writes
    (plan.record_value_problem); all before any file is read and with the
    record unchanged.
    """
    stream, quiet = _progress_options(progress)
    key = Path(os.path.abspath(os.fspath(source)))
    folder = key.parent
    if record.get("status") not in VERIFIABLE_STATUSES:
        raise ValueError("record {} of '{}' has status '{}'; verify needs 'converted' or 'verified'".format(
            record.get("id"), key.name, record.get("status")))
    problem = record_name_problem(record)
    if problem is not None:
        raise RecordRefused("record of '{}' is refused: {}".format(key.name, problem))
    if _option(options, "ssim_check", False) and record.get("action") == "reencode":
        # Only then do record values (video_filters) reach an ffmpeg argv; checked as convert checks them.
        try:
            problem = record_value_problem(record)
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as error:
            problem = "the record's values cannot be checked: {}".format(error)
        if problem is not None:
            raise RecordRefused("record of '{}' is refused for --ssim-check: {}".format(key.name, problem))
    _archive_previous(record)
    output_path = output_path_of(folder, record)
    stage = {"output_name": output_path.name if output_path is not None else None}
    if output_path is None or not output_path.is_file():
        stage["error"] = "output missing"
        add_stage(record, "verify", stage)
        set_status(record, "failed")
        save_record(key, record)
        say("  verify failed: output '{}' is missing".format(stage["output_name"]), stream)
        return record

    # Fingerprint and hash first, so the SHA-256 stored is of the bytes the checks below read; the
    # fingerprint is taken again after the last check and a difference fails the record.
    fingerprint = cheap_fingerprint(output_path)
    size = fingerprint["size"]
    say("verifying '{}' ({})".format(output_path.name, format_bytes(size)), stream)
    if size > HASH_PROGRESS_BYTES:
        say("  hashing '{}' ({})".format(output_path.name, format_bytes(size)), stream)
        with Progress("sha256", total=size, unit="B", stream=stream, quiet=quiet) as live:
            digest = sha256_file(output_path, progress=live)
    else:
        digest = sha256_file(output_path)
    try:
        probe = probe_file(tools, output_path)
    except FfmpegError as error:
        return _fail(record, key, stage, "ffprobe cannot read the output: {}".format(error), stream)
    output_duration = (probe.get("format") or {}).get("duration")
    video = probe.get("video") or {}

    with Progress("verify decode", total=output_duration, unit="s", stream=stream, quiet=quiet) as live:
        decode = decode_output(tools, output_path, live, output_duration)
    stage["decode"] = decode
    if decode["returncode"] != 0 and not decode["frames_decoded"]:
        return _fail(record, key, stage, "the output does not decode (ffmpeg status {}, no frame)".format(
            decode["returncode"]), stream)

    stage["output_probe"] = {
        "duration": output_duration,
        "streams": actual_streams(probe),
        "video": {name: video.get(name) for name in ("codec", "profile", "level", "pix_fmt", "width", "height",
                                                     "sar", "dar", "fps", "fps_fraction", "field_order")},
        "audio": [{name: entry.get(name) for name in ("codec", "channels", "sample_rate")}
                  for entry in probe.get("audio") or []],
        "subtitles": [{name: entry.get(name) for name in ("codec", "language")}
                      for entry in probe.get("subtitles") or []],
    }
    identity = check_identity(record, read_tag(probe))
    stage["identity"] = identity

    try:
        actual_frames = (count_frames(tools, output_path, video["index"], progress)
                         if video.get("index") is not None else None)
    except FfmpegError as error:
        say("  could not count output frames: {}".format(error), stream)
        actual_frames = None
    measured, how = _measure_source_frames(tools, folder, record, stream, progress)
    frames = compare_frames(expected_frames(record, measured), actual_frames)
    frames.update(source_frames=measured, source_frames_from=how,
                  fps_multiplier=_cadence(record).get("fps_multiplier"), cadence=_cadence(record).get("kind"))
    stage["frames"] = frames
    source_seconds, source_kind = source_video_duration(record)
    output_seconds, output_kind = output_video_duration(probe)
    stage["duration"] = compare_durations(source_seconds, output_seconds, duration_tolerance(video.get("fps")))
    stage["duration"].update(source_kind=source_kind, output_kind=output_kind)
    stage["streams"] = compare_streams(expected_streams(record), probe)

    threshold = float(_option(options, "ssim_threshold", DEFAULT_SSIM_THRESHOLD))
    if _option(options, "ssim_check", False):
        windows = sample_windows(output_duration, int(_option(options, "samples", DEFAULT_SAMPLES)), WINDOW_SECONDS)
        action = record.get("action")
        filters = video_filters(_analysis(record), action) if action == "reencode" else None
        say("  sampled SSIM over {} window(s)".format(len(windows)), stream)
        with Progress("ssim", total=len(windows), unit="items", stream=stream, quiet=quiet) as live:
            value = sampled_ssim(tools, source_inputs(folder, record), output_path, windows, live, filters,
                                 video.get("fps_fraction"))
        stage["ssim"] = {"requested": True, "value": value, "threshold": threshold, "windows": windows,
                         "ok": value is not None and value >= threshold}
    else:
        stage["ssim"] = {"requested": False, "value": None, "threshold": threshold, "windows": [], "ok": None}

    try:
        after = cheap_fingerprint(output_path)
    except OSError as error:
        after = None
        say("  cannot fingerprint '{}' again: {}".format(output_path.name, error), stream)
    # Same machine, same run: the time is compared too (fingerprint_identical), since a write to
    # the middle of the file during the checks changes only the time.
    if not fingerprint_identical(fingerprint, after):
        stage["output_changed_during_verify"] = True
        stage["fingerprint_before"] = fingerprint
        stage["fingerprint_after"] = after
        record.pop("score", None)
        return _fail(record, key, stage, "output '{}' changed while it was being verified; "
                     "its checks and hash do not describe one file".format(output_path.name), stream)
    convert = _stages(record).get("convert") or {}
    source_size = convert.get("source_size") or (record.get("source") or {}).get("size")
    stage["size"] = {"output": size, "source": source_size,
                     "ratio": round(float(size) / source_size, 4) if source_size else None,
                     "larger": bool(source_size) and size > source_size}

    output = record.setdefault("output", {})
    output.update(sha256=digest, size=size, fingerprint=fingerprint)

    checks = {"decode": decode, "duration": stage["duration"], "frames": frames, "ssim": stage["ssim"],
              "identity": identity, "streams": stage["streams"]}
    score, added = score_record(record, checks)
    for entry in added:
        record.setdefault("penalties", []).append(dict(entry, stage=VERIFY_STAGE_MARK))
    flags = list(decode.get("flags") or [])
    if not identity["ok"]:
        flags.append("identity-mismatch")
    if not stage["streams"]["ok"]:
        flags.append("stream-count-mismatch")
    flags_added = [flag for flag in flags if flag not in (record.get("flags") or [])]
    for flag in flags_added:
        add_flag(record, flag)
    stage.update(penalties=added, flags_added=flags_added, score=score, band=band(score))
    record["score"] = score
    add_stage(record, "verify", stage)
    set_status(record, "verified")
    save_record(key, record)
    say("  verified '{}': score {} ({}); frames {} of {} expected, duration delta {} s{}".format(
        output_path.name, score, band(score), frames.get("actual"), frames.get("expected"),
        stage["duration"].get("delta"), "" if not added else "; " + ", ".join(
            "{} -{}".format(entry["code"], entry["points"]) for entry in added)), stream)
    return record


# --- command line ---------------------------------------------------------------------


def _positive_int(text):
    import argparse
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("'{}' is not a whole number".format(text))
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more, not {}".format(value))
    return value


def _unit_fraction(text):
    import argparse
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("'{}' is not a number".format(text))
    if not 0.0 < value <= 1.0:
        raise argparse.ArgumentTypeError("must be above 0 and at most 1, not {}".format(value))
    return value


def add_arguments(parser):
    parser.add_argument("paths", nargs="*", metavar="PATH",
                        help="folders to verify (walked for records), or source or output files "
                        "(default: the current directory)")
    parser.add_argument("--ssim-check", action="store_true",
                        help="also compare sampled windows with the source by SSIM on luma (slow)")
    parser.add_argument("--ssim-threshold", type=_unit_fraction, default=DEFAULT_SSIM_THRESHOLD, metavar="F",
                        help="SSIM below F costs 25 points (default 0.97)")
    parser.add_argument("--samples", type=_positive_int, default=None, metavar="N",
                        help="SSIM sample windows (default {})".format(DEFAULT_SAMPLES))
    return parser


def _records_under(path, stream):
    """(folder, record) pairs under a PATH: a folder is walked; a file means the records of its folder naming it."""
    path = Path(os.path.abspath(path))
    if path.is_dir():
        return list(iter_records(path, on_error=lambda entry, error: say("skipping record: {}".format(error),
                                                                           stream)))
    found = []
    folder = path.parent
    for record_folder, record in iter_records(folder, on_error=lambda entry, error: None):
        if record_folder != folder:
            continue
        # A list, not a set: an edited record's names may be unhashable; run() refuses a record whose names are
        # not plain names.
        source, output = record.get("source"), record.get("output")
        names = [source.get("name")] if isinstance(source, dict) else []
        if isinstance(output, dict):
            names += [output.get(name) for name in ("name", "interim_name")]
        if path.name in names:
            found.append((record_folder, record))
    return found


def run(args):
    """`vidfunnel verify`: EXIT_OK, EXIT_ERROR when any record failed, EXIT_USAGE, EXIT_DEPS."""
    from vidfunnel.cli import EXIT_DEPS, EXIT_ERROR, EXIT_OK, EXIT_USAGE, paths_or_current_directory
    from vidfunnel.deps import DependencyError, require
    paths_or_current_directory(args, "verify")
    stream = getattr(args, "stream", None)
    try:
        tools = require(args)
    except DependencyError as error:
        print("vidfunnel verify: {}".format(error), file=sys.stderr)
        return EXIT_DEPS
    missing = [path for path in args.paths if not os.path.exists(path)]
    if missing:
        for path in missing:
            print("vidfunnel verify: '{}' does not exist".format(path), file=sys.stderr)
        return EXIT_USAGE
    summary = StageSummary("verify", OUTCOMES)
    by_folder = {}
    order = []
    seen = set()
    for path in args.paths:
        for folder, record in _records_under(path, stream):
            # repr: an edited record's id may be unhashable; such a record is verified and its identity check fails.
            identity = (str(folder), repr(record.get("id")))
            if identity in seen:
                continue
            seen.add(identity)
            if record.get("status") in VERIFIABLE_STATUSES:
                # Before the record is keyed: a source name with a directory part would key another folder's record.
                problem = record_name_problem(record)
                if problem is not None:
                    say("skipping a record in '{}': {}".format(folder, problem), stream)
                    summary.add("skipped")
                    continue
            if str(folder) not in by_folder:
                by_folder[str(folder)] = []
                order.append(str(folder))
            by_folder[str(folder)].append(record)
    wanted_in = {folder: [record for record in by_folder[folder] if record.get("status") in VERIFIABLE_STATUSES]
                 for folder in order}
    total = sum(len(wanted_in[folder]) for folder in order)
    position = 0
    drained = False
    for folder_number, folder in enumerate(order):
        records = by_folder[folder]
        wanted = wanted_in[folder]
        if len(wanted) < len(records):
            summary.add("skipped", len(records) - len(wanted))
        if not wanted:
            continue
        later = sum(len(wanted_in[after]) for after in order[folder_number + 1:])
        # A drain (SIGUSR1, a stop file) starts no further item; a 'now' stop file raises KeyboardInterrupt here.
        if drain_before_next(len(wanted) + later, folder):
            break
        first_position = position
        try:
            with FolderLock(folder, warn=lambda message: say(message, stream)):
                for item_number, collected in enumerate(wanted):
                    if drain_before_next(len(wanted) - item_number + later, folder):
                        drained = True
                        break
                    key = record_key(folder, collected)
                    position += 1
                    # Every line of this item starts with its counter, as convert's do.
                    tag = item_tag("verify", position, total)
                    item = types.SimpleNamespace(stream=tagged(stream, tag),
                                                 quiet=bool(getattr(args, "quiet", False)))
                    # Read again under the lock: a retire or convert that finished since the
                    # collection above has changed the record on disk, and verifying the stale
                    # copy would overwrite that change.
                    try:
                        record = load_record(key)
                    except RecordError as error:
                        say("failed: '{}': {}".format(key.name, error), item.stream)
                        summary.add("failed")
                        drain_after_item()
                        continue
                    if record is None or record.get("status") not in VERIFIABLE_STATUSES:
                        say("skipping '{}': its record {} since it was listed".format(
                            key.name, "is gone" if record is None
                            else "now has status '{}'".format(record.get("status"))), item.stream)
                        summary.add("skipped")
                        drain_after_item()
                        continue
                    try:
                        result = verify_record(tools, key, record, args, progress=item)
                    except RecordRefused as error:
                        # Changed on disk since it was collected; left as it is.
                        say("skipping '{}': {}".format(key.name, error), item.stream)
                        summary.add("skipped")
                        drain_after_item()
                        continue
                    except (OSError, RecordError, FfmpegError) as error:
                        say("failed: '{}': {}".format(key.name, error), item.stream)
                        summary.add("failed")
                        drain_after_item()
                        continue
                    drain_after_item()
                    if result.get("status") == "failed":
                        summary.add("failed")
                    else:
                        summary.add(band(result.get("score")) or "hold")
        except LockHeld as error:
            say("skipping folder '{}': {}".format(folder, error), stream)
            summary.add("skipped", len(wanted))
            position = first_position + len(wanted)
        if drained:
            break
    say(summary.format("'{}/' under each folder".format(RECORD_DIR)), stream)
    return EXIT_ERROR if summary.counts.get("failed") else EXIT_OK

# End of File #
