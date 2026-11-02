"""Analysis stage: sampled idet and cropdetect, aspect, cadence, sync, colour, pixel art, audio, subtitles.

HANDOFF.md sections 2.1 to 2.3, 3, 4 and 5. Every decision is a dict

    {"result": ..., "reason": str, "penalty": int, "flags": [str],
     "samples": [[start_s, length_s or None], ...], "penalties": [...]}

plus result-specific keys. 'penalty' is the number of points the decision
takes off the predicted score (positive, as in record.add_penalty: 30 means
"-30" in HANDOFF's tables); 'penalties' lists the same points as
{"code", "points", "reason"} entries, ready for the record. 'samples' holds
the windows the decision was measured on: the sampled windows for cropdetect
and idet, [[0.0, None]] (the whole file) for the decode check and the packet
scans, and [] for decisions taken from stream metadata alone.

The decide_* functions are pure: they take probe data and measurements and
are unit-tested from captured JSON. The *_run functions and measure_durations
call ffmpeg or ffprobe. analyze() runs everything for one file and returns
the dict that plan stores under stages.analyze.

Readings of HANDOFF that are choices, not quotations, are documented on the
function that implements them (aspect tie-break, idet window classes, the
packet-quantisation tolerance on drift, audio priming).
"""

VERSION = "20261102"

import math
import os
import re
from fractions import Fraction

from vidfunnel.ffmpeg import ffprobe_json, ffprobe_packets, null_decode, run
from vidfunnel.probe import keyframe_spacing
from vidfunnel.progress import Progress, ffmpeg_progress_callback, say

# --- sampling (HANDOFF 4) -------------------------------------------------------------

DEFAULT_SAMPLES = 12
WINDOW_SECONDS = 3.0
EDGE_SKIP_SHARE = 0.03
FULL_ANALYSIS_BELOW_S = 60.0

# --- aspect (HANDOFF 4.2) -------------------------------------------------------------

NON_SQUARE_SIZES = (
    (720, 480), (704, 480), (352, 480), (352, 240), (480, 480),
    (720, 576), (704, 576), (352, 576), (352, 288), (480, 576),
)
NTSC_HEIGHTS = (480, 240)
PAL_HEIGHTS = (576, 288)
# Candidate SARs per family, in tie-break order: 4:3 first.
CANDIDATE_SARS = {
    "ntsc": (("4:3", "8:9"), ("16:9", "32:27")),
    "pal": (("4:3", "16:15"), ("16:9", "64:45")),
}
# HANDOFF lists 1.33 and 1.78; those are 4:3 and 16:9 rounded, so the exact
# values are used. 1.85, 2.35 and 2.39 are exact as listed.
STANDARD_RATIOS = (("4:3", 4.0 / 3.0), ("16:9", 16.0 / 9.0), ("1.85:1", 1.85), ("2.35:1", 2.35), ("2.39:1", 2.39))
STANDARD_RATIO_TOLERANCE = 0.02
# Two candidates whose distances to their nearest standard ratio differ by
# less than this are a tie, broken in favour of 4:3.
RATIO_TIE = 1e-6
CROP_AGREEMENT_SHARE = 0.75
CROP_AGREEMENT_PIXELS = 2
PENALTY_ASPECT_FROM_BARS = 15
PENALTY_ASPECT_AMBIGUOUS = 30

# --- interlace and cadence (HANDOFF 4.3) ----------------------------------------------

INTERLACED_FIELD_ORDERS = {"tt": "tff", "bt": "tff", "bb": "bff", "tb": "bff"}
TELECINE_REPEAT_SHARE = 0.15
# A window that is not interlaced-looking still counts as "mixed-looking" for
# the telecine test when at least this share of its determined frames
# (TFF + BFF + progressive) is combed; 3:2 pulldown combs 2 frames in 5.
MIXED_WINDOW_COMBED_SHARE = 0.2
NTSC_FRAME_RATE = 30000.0 / 1001.0
FRAME_RATE_TOLERANCE = 0.01
HD_HEIGHT_ABOVE = 576
PENALTY_TELECINED = 10
PENALTY_INTERLACED = 5
PENALTY_CADENCE_MIXED = 40

# --- sync (HANDOFF 4.4) ---------------------------------------------------------------

SYNC_OFFSET_S = 0.1
DRIFT_NONE_BELOW = 0.001
DRIFT_CORRECT_UP_TO = 0.02
PENALTY_SYNC_DRIFT = 20
PENALTY_SYNC_DRIFT_LARGE = 100
# An audio gap is a jump between successive packets larger than this share
# of the stream's typical packet duration (half a packet: at least one whole
# packet is missing; Matroska's millisecond rounding stays well under it).
AUDIO_GAP_PACKET_SHARE = 0.5
PACKET_ENTRIES = "pts_time,dts_time,duration_time:packet_side_data=skip_samples,discard_padding"

# --- decode check (HANDOFF 4.1, 5) ----------------------------------------------------

PENALTY_DECODE_CLASS = 10
# Message classes that say nothing about damage: recorded under 'ignored',
# no points. ffmpeg prints the first for every PCM/WMA stream without a
# channel layout, the second for every MPEG-PS file it reads.
BENIGN_DECODE_MESSAGES = (
    "Guessed Channel Layout",
    "start time for stream # is not set in estimate_timings_from_pts",
)

# --- colour, pixel art, audio, subtitles ----------------------------------------------

SD_MAX_HEIGHT = 576
PAL_FRAME_RATES = (25.0, 50.0)
PALETTE_CODECS = ("msrle", "msvideo1", "qtrle", "rpza", "smc", "cinepak", "flic", "8bps", "qtanimation", "zmbv",
                  "gif")
PIXELART_MAX_HEIGHT = 300
PIXELART_TRIPLE_BELOW = 160
AUDIO_BITRATE_PER_CHANNEL_K = 96
AUDIO_BITRATE_CAP_K = 448
TEXT_SUBTITLE_CODECS = ("subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text")
STYLED_SUBTITLE_CODECS = ("ass", "ssa")
BITMAP_SUBTITLE_SIDECARS = {"dvd_subtitle": ".subs.mkv", "hdmv_pgs_subtitle": ".sup"}

# --- remux eligibility (HANDOFF 3) ----------------------------------------------------

REMUX_PROFILES = ("Baseline", "Constrained Baseline", "Main", "High")
REMUX_MAX_LEVEL = 41
LONG_GOP_S = 5.0


# --- helpers ----------------------------------------------------------------------------


def _decision(result, reason, penalties=(), flags=(), samples=(), **extra):
    """One decision dict. penalties: iterable of (code, points, reason)."""
    entries = [{"code": code, "points": int(points), "reason": why} for code, points, why in penalties]
    decision = {
        "result": result,
        "reason": reason,
        "penalty": sum(entry["points"] for entry in entries),
        "penalties": entries,
        "flags": _unique(flags),
        "samples": [list(window) for window in samples],
    }
    decision.update(extra)
    return decision


def _unique(items):
    seen = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _ratio_parts(text):
    """'8:9' -> (8, 9); None for None or malformed."""
    if not text:
        return None
    match = re.match(r"^(\d+):(\d+)$", text)
    if match is None or int(match.group(2)) == 0:
        return None
    return int(match.group(1)), int(match.group(2))


def _ratio_text(fraction):
    return "{}:{}".format(fraction.numerator, fraction.denominator)


def _number(value):
    """float(value) or None for None, 'N/A' and garbage."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _median(values):
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _near(value, target, tolerance=FRAME_RATE_TOLERANCE):
    return value is not None and abs(value - target) <= tolerance


def _name(path):
    return os.path.basename(os.fspath(path))


def _progress_options(progress):
    """(stream, quiet) from the `progress` argument: None, or any object with 'stream' and 'quiet'
    attributes (the parsed CLI args work)."""
    if progress is None:
        return None, False
    return getattr(progress, "stream", None), bool(getattr(progress, "quiet", False))


def _window_args(window):
    start, length = window
    args = []
    if length is not None:
        args.extend(["-ss", "{:.3f}".format(start), "-t", "{:.3f}".format(length)])
    elif start:
        args.extend(["-ss", "{:.3f}".format(start)])
    return args


# --- sampling -------------------------------------------------------------------------


def sample_windows(duration, samples=DEFAULT_SAMPLES, window=WINDOW_SECONDS, full=False):
    """Sample windows as (start_s, length_s); length None means "to the end". Pure.

    `samples` windows of `window` seconds, evenly spaced from 3 % into the
    file to 3 % before its end (the last window ends there). `full`, a
    duration under 60 s or an unknown duration gives one window over the
    whole file: [(0.0, None)].
    """
    if full or duration is None or duration < FULL_ANALYSIS_BELOW_S:
        return [(0.0, None)]
    count = max(1, int(samples))
    first = duration * EDGE_SKIP_SHARE
    last = duration * (1.0 - EDGE_SKIP_SHARE) - window
    if last <= first or count == 1:
        middle = max(0.0, (duration - window) / 2.0)
        return [(round(middle, 3), window)]
    step = (last - first) / (count - 1)
    return [(round(first + step * position, 3), window) for position in range(count)]


# --- decode check ---------------------------------------------------------------------


def decode_run(tools, path, progress=None, duration=None):
    """The full-file decode check (HANDOFF 4.1): vidfunnel.ffmpeg.null_decode with live progress."""
    stream, quiet = _progress_options(progress)
    say("decode check of '{}'".format(_name(path)), stream)
    with Progress("decode", total=duration, unit="s", stream=stream, quiet=quiet) as live:
        return null_decode(tools, path, progress=ffmpeg_progress_callback(live, duration))


def decide_decode(decode_result):
    """Decode-check decision from a DecodeResult (or any object with returncode and message_classes). Pure.

    Every message class is kept with its count and costs 10 points
    (HANDOFF 5); flag 'decode-warnings' when there is any, 'decode-failed'
    when ffmpeg exited non-zero. Classes containing one of
    BENIGN_DECODE_MESSAGES are kept under 'ignored' and cost nothing.
    """
    classes = {}
    ignored = {}
    for message, count in (decode_result.message_classes or {}).items():
        if any(benign in message for benign in BENIGN_DECODE_MESSAGES):
            ignored[message] = count
        else:
            classes[message] = count
    penalties = [("decode-warnings", PENALTY_DECODE_CLASS, "decoder message x{}: {}".format(count, message))
                 for message, count in sorted(classes.items())]
    flags = []
    if classes:
        flags.append("decode-warnings")
    if decode_result.returncode != 0:
        flags.append("decode-failed")
    if decode_result.returncode != 0:
        reason = "ffmpeg exited with status {} while decoding; {} message class(es)".format(
            decode_result.returncode, len(classes))
    elif classes:
        reason = "{} decoder message class(es), {} message(s) in all".format(len(classes), sum(classes.values()))
    else:
        reason = "decoded with no warnings"
    if ignored:
        reason += "; {} benign class(es) not counted".format(len(ignored))
    return _decision({"returncode": decode_result.returncode, "classes": classes, "ignored": ignored}, reason,
                     penalties, flags,
                     [(0.0, None)])


# --- cropdetect -----------------------------------------------------------------------

_CROP = re.compile(r"crop=(-?\d+):(-?\d+):(-?\d+):(-?\d+)")


def parse_cropdetect(stderr):
    """The last 'crop=W:H:X:Y' in ffmpeg stderr as {'w','h','x','y'}, or None. Pure."""
    matches = _CROP.findall(stderr or "")
    if not matches:
        return None
    width, height, left, top = (int(value) for value in matches[-1])
    return {"w": width, "h": height, "x": left, "y": top}


def cropdetect_run(tools, path, windows, progress=None):
    """cropdetect=limit=24:round=2:reset=0 over each window; per-window crop dict or None (HANDOFF 4.2 step 3).

    A window in which cropdetect printed nothing (no frame decoded) is None.
    Raises FfmpegError when ffmpeg fails.
    """
    stream, quiet = _progress_options(progress)
    absolute = os.path.abspath(os.fspath(path))
    say("cropdetect over {} window(s) of '{}'".format(len(windows), _name(path)), stream)
    results = []
    with Progress("cropdetect window", total=len(windows), unit="items", stream=stream, quiet=quiet) as live:
        for position, window in enumerate(windows):
            live.update(position)
            argv = [tools.ffmpeg, "-hide_banner", "-nostats"] + _window_args(window) + [
                "-i", absolute, "-map", "0:v:0", "-an", "-sn", "-dn",
                "-vf", "cropdetect=limit=24:round=2:reset=0", "-f", "null", "-"]
            completed = run(argv)
            results.append(parse_cropdetect(completed.stderr))
        live.update(len(windows))
    return results


def _crops_agree(first, second):
    return all(abs(first[key] - second[key]) <= CROP_AGREEMENT_PIXELS for key in ("w", "h", "x", "y"))


def aggregate_crop(per_window, width, height):
    """The crop at least 75 % of all windows agree on within 2 px, or None for "no bars". Pure.

    Windows with no result count as disagreeing. The agreed crop is the
    window result with the most others within 2 px of it (the first such on
    a tie). A crop that leaves the full frame (within 2 px) means no bars and
    also gives None.
    """
    if not per_window:
        return None
    present = [crop for crop in per_window if crop is not None]
    best = None
    best_count = 0
    for candidate in present:
        count = sum(1 for other in present if _crops_agree(candidate, other))
        if count > best_count:
            best, best_count = candidate, count
    if best is None or best_count < CROP_AGREEMENT_SHARE * len(per_window):
        return None
    if width is not None and height is not None:
        if (best["w"] >= width - CROP_AGREEMENT_PIXELS and best["h"] >= height - CROP_AGREEMENT_PIXELS):
            return None
    if best["w"] <= 0 or best["h"] <= 0:
        return None
    return dict(best)


# --- aspect ---------------------------------------------------------------------------


def _family(height):
    if height in NTSC_HEIGHTS:
        return "ntsc"
    if height in PAL_HEIGHTS:
        return "pal"
    return None


def _nearest_standard(ratio):
    """(name, relative distance) of the standard ratio nearest to `ratio`."""
    best = None
    for name, value in STANDARD_RATIOS:
        distance = abs(ratio / value - 1.0)
        if best is None or distance < best[1]:
            best = (name, distance)
    return best


def needs_cropdetect(video_stream):
    """True when the aspect rule needs bar evidence: SAR 1:1 or unknown on a non-square frame size."""
    if video_stream is None:
        return False
    sar = video_stream.get("sar")
    size = (video_stream.get("width"), video_stream.get("height"))
    return sar in (None, "1:1") and size in NON_SQUARE_SIZES


def decide_aspect(video_stream, crop, assume_dar=None):
    """Aspect decision (HANDOFF 4.2). Pure.

    - A signalled SAR other than 1:1 is kept ('signalled', no penalty).
    - SAR 1:1 or unknown on a size outside the non-square table: square
      pixels ('square', no penalty).
    - SAR 1:1 or unknown on a table size: the candidates are 4:3 and 16:9
      (SAR 8:9 / 32:27 for 480 and 240 lines, 16:15 / 64:45 for 576 and 288).
      With bars (`crop`, the agreed cropdetect result) the active area's DAR
      is computed under each candidate SAR; candidates within 2 % of a
      standard ratio (4:3, 16:9, 1.85, 2.35, 2.39) qualify, the one closest
      to its standard ratio wins and an exact tie goes to 4:3 ('bars', 15
      points). Example: a 720x360 active area gives 640/360 = 1.778 under
      8:9 (0.0 % from 16:9) and 853/360 = 2.370 under 32:27 (0.8 % from
      2.35), so 4:3 wins. When no candidate lands on a standard ratio the
      bars do not decide and the no-bars rule applies.
    - No bars: `assume_dar` ('4:3' or '16:9') resolves it ('assumed', no
      penalty); otherwise the signalled values stay, flag 'aspect-ambiguous',
      30 points.

    Result: {'sar': 'W:H', 'dar': 'W:H', 'source': 'signalled' | 'square' |
    'bars' | 'assumed' | 'ambiguous', 'candidate': '4:3' | '16:9' | None}.
    'dar' is the display ratio of the whole frame under 'sar', reduced.
    """
    width = video_stream.get("width")
    height = video_stream.get("height")
    sar = video_stream.get("sar")
    dar = video_stream.get("dar")

    def frame_dar(sar_text):
        parts = _ratio_parts(sar_text)
        if parts is None or not width or not height:
            return dar
        return _ratio_text(Fraction(width * parts[0], height * parts[1]))

    def result(sar_text, source, candidate=None):
        return {"sar": sar_text, "dar": frame_dar(sar_text), "source": source, "candidate": candidate}

    if sar is not None and sar != "1:1":
        return _decision(result(sar, "signalled"), "signalled SAR {} (DAR {}) kept".format(sar, dar), crop=crop)
    if (width, height) not in NON_SQUARE_SIZES:
        return _decision(result("1:1", "square"),
                         "{}x{} is not a non-square frame size; SAR {} means square pixels".format(
                             width, height, sar or "unknown"), crop=crop)

    family = _family(height)
    candidates = CANDIDATE_SARS[family]
    bars_note = "no bars agreed across the windows"
    if crop is not None:
        scored = []
        notes = []
        for position, (label, candidate_sar) in enumerate(candidates):
            numerator, denominator = _ratio_parts(candidate_sar)
            active = crop["w"] * numerator / float(crop["h"] * denominator)
            standard, distance = _nearest_standard(active)
            notes.append("{} (SAR {}): active {:.3f}, {:.1f} % from {}".format(
                label, candidate_sar, active, distance * 100.0, standard))
            if distance <= STANDARD_RATIO_TOLERANCE:
                scored.append((distance, position, label, candidate_sar))
        if scored:
            scored.sort()
            best = scored[0]
            if len(scored) > 1 and abs(scored[1][0] - best[0]) < RATIO_TIE:
                best = min(scored[:2], key=lambda entry: entry[1])
            reason = "SAR {} on {}x{} with bars {}x{} at {},{}: {}; chose {}".format(
                sar or "unknown", width, height, crop["w"], crop["h"], crop["x"], crop["y"],
                "; ".join(notes), best[2])
            return _decision(result(best[3], "bars", best[2]), reason,
                             [("aspect-from-bars", PENALTY_ASPECT_FROM_BARS,
                               "aspect {} inferred from letterbox bars".format(best[2]))], crop=crop)
        bars_note = "bars {}x{} land on no standard ratio under either candidate ({})".format(
            crop["w"], crop["h"], "; ".join(notes))

    if assume_dar is not None:
        for label, candidate_sar in candidates:
            if label == assume_dar:
                return _decision(result(candidate_sar, "assumed", label),
                                 "SAR {} on {}x{}, {}; --assume-dar {} gives SAR {}".format(
                                     sar or "unknown", width, height, bars_note, label, candidate_sar), crop=crop)
        raise ValueError("assume_dar must be '4:3' or '16:9', not '{}'".format(assume_dar))
    reason = "SAR {} on {}x{} (a non-square size), {}: 4:3 and 16:9 cannot be told apart; left as signalled".format(
        sar or "unknown", width, height, bars_note)
    return _decision(result(sar or "1:1", "ambiguous"), reason,
                     [("aspect-ambiguous", PENALTY_ASPECT_AMBIGUOUS, "4:3 or 16:9 undecidable without bars")],
                     ["aspect-ambiguous"], crop=crop)


# --- idet -----------------------------------------------------------------------------

_IDET_MULTI = re.compile(
    r"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)\s*Undetermined:\s*(\d+)")
_IDET_SINGLE = re.compile(
    r"Single frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)\s*Undetermined:\s*(\d+)")
_IDET_REPEATED = re.compile(r"Repeated Fields:\s*Neither:\s*(\d+)\s*Top:\s*(\d+)\s*Bottom:\s*(\d+)")


def parse_idet(stderr):
    """idet counters from ffmpeg stderr, or None when idet printed no multi-frame line. Pure.

    ffmpeg prints the summary once for an unused filter instance (all
    zeros) and once for the real one; the last occurrence of each line is
    used. Returns {'tff','bff','progressive','undetermined' (multi-frame),
    'single_tff','single_bff','single_progressive','single_undetermined',
    'repeated_neither','repeated_top','repeated_bottom'}.
    """
    text = stderr or ""
    multi = _IDET_MULTI.findall(text)
    if not multi:
        return None
    counters = dict(zip(("tff", "bff", "progressive", "undetermined"), (int(value) for value in multi[-1])))
    single = _IDET_SINGLE.findall(text)
    values = single[-1] if single else ("0", "0", "0", "0")
    counters.update(zip(("single_tff", "single_bff", "single_progressive", "single_undetermined"),
                        (int(value) for value in values)))
    repeated = _IDET_REPEATED.findall(text)
    values = repeated[-1] if repeated else ("0", "0", "0")
    counters.update(zip(("repeated_neither", "repeated_top", "repeated_bottom"), (int(value) for value in values)))
    return counters


def idet_run(tools, path, windows, progress=None):
    """idet over each window; per-window counters from parse_idet (None when idet saw no frame)."""
    stream, quiet = _progress_options(progress)
    absolute = os.path.abspath(os.fspath(path))
    say("idet over {} window(s) of '{}'".format(len(windows), _name(path)), stream)
    results = []
    with Progress("idet window", total=len(windows), unit="items", stream=stream, quiet=quiet) as live:
        for position, window in enumerate(windows):
            live.update(position)
            argv = [tools.ffmpeg, "-hide_banner", "-nostats"] + _window_args(window) + [
                "-i", absolute, "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", "idet", "-f", "null", "-"]
            completed = run(argv)
            results.append(parse_idet(completed.stderr))
        live.update(len(windows))
    return results


def classify_idet_window(counters):
    """(kind, order) of one window: 'interlaced', 'telecined' or 'progressive'; order 'tff'/'bff'/None. Pure.

    Multi-frame counts are used. Interlaced: TFF+BFF above both progressive
    and undetermined. Telecined: repeated top+bottom fields are at least 15 %
    of the window's frames (Neither+Top+Bottom) and the window is
    interlaced-looking or mixed-looking (combed frames at least 20 % of the
    determined frames TFF+BFF+progressive). Progressive otherwise.
    Undetermined frames never count as interlaced: an all-undetermined
    window is progressive.
    """
    if counters is None:
        return ("progressive", None)
    combed = counters["tff"] + counters["bff"]
    order = None
    if combed:
        order = "tff" if counters["tff"] >= counters["bff"] else "bff"
    interlaced_looking = combed > counters["progressive"] and combed > counters["undetermined"]
    determined = combed + counters["progressive"]
    mixed_looking = combed > 0 and determined > 0 and combed >= MIXED_WINDOW_COMBED_SHARE * determined
    repeated = counters.get("repeated_top", 0) + counters.get("repeated_bottom", 0)
    frames = repeated + counters.get("repeated_neither", 0)
    if frames <= 0:
        frames = combed + counters["progressive"] + counters["undetermined"]
    if (interlaced_looking or mixed_looking) and frames > 0 and repeated >= TELECINE_REPEAT_SHARE * frames:
        return ("telecined", order)
    if interlaced_looking:
        return ("interlaced", order)
    return ("progressive", None)


def aggregate_idet(per_window, fps=None):
    """Whole-file idet summary from per-window counters. Pure.

    'progressive' when every window is; 'telecined' when every window that is
    not progressive is telecined-looking and the frame rate is 29.97 (fps
    None, unknown, also allows it); 'interlaced' when every window is
    interlaced with one field order; 'mixed' otherwise (interlaced and
    progressive windows together, disagreeing field orders, interlaced and
    telecined windows together, or telecine at a rate other than 29.97).

    Returns {'kind', 'order', 'reason', 'windows': [{'counters', 'kind',
    'order'}], 'totals': {summed counters}, 'determined_frames': TFF+BFF+
    progressive summed (0 means idet gave no evidence either way)}.
    """
    windows = []
    totals = {}
    for counters in per_window:
        kind, order = classify_idet_window(counters)
        windows.append({"counters": counters, "kind": kind, "order": order})
        for key, value in (counters or {}).items():
            totals[key] = totals.get(key, 0) + value
    determined = totals.get("tff", 0) + totals.get("bff", 0) + totals.get("progressive", 0)
    kinds = [entry["kind"] for entry in windows]
    not_progressive = [entry for entry in windows if entry["kind"] != "progressive"]
    orders = sorted(set(entry["order"] for entry in not_progressive if entry["order"]))
    majority = None
    if totals.get("tff", 0) or totals.get("bff", 0):
        majority = "tff" if totals.get("tff", 0) >= totals.get("bff", 0) else "bff"
    count_text = ", ".join("{} {}".format(kinds.count(name), name)
                           for name in ("progressive", "interlaced", "telecined") if kinds.count(name))
    count_text = "{} window(s): {}".format(len(windows), count_text or "none measured")

    if not not_progressive:
        kind, order = "progressive", None
        reason = count_text
    elif all(entry["kind"] == "telecined" for entry in not_progressive):
        if fps is None or _near(fps, NTSC_FRAME_RATE):
            kind, order = "telecined", majority
            reason = count_text
        else:
            kind, order = "mixed", majority
            reason = "{}; telecine pattern at {:.3f} fps, not 29.97".format(count_text, fps)
    elif all(entry["kind"] == "interlaced" for entry in not_progressive) and len(not_progressive) == len(windows):
        if len(orders) == 1:
            kind, order = "interlaced", orders[0]
            reason = count_text
        else:
            kind, order = "mixed", majority
            reason = "{}; field orders disagree ({})".format(count_text, ", ".join(orders))
    else:
        kind, order = "mixed", majority
        reason = "{}; inconsistent across windows".format(count_text)
    return {"kind": kind, "order": order, "reason": reason, "windows": windows, "totals": totals,
            "determined_frames": determined}


def _double_rate(fraction_text):
    if not fraction_text:
        return None
    try:
        return _ratio_text(Fraction(fraction_text) * 2).replace(":", "/")
    except (ValueError, ZeroDivisionError):
        return None


def decide_cadence(idet_summary, video_stream, max_level="4.2"):
    """Interlace and cadence decision (HANDOFF 4.3). Pure.

    The container is trusted when idet gave no evidence (no frame judged
    TFF, BFF or progressive) and the stream's field_order is interlaced
    (tt/bt -> tff, bb/tb -> bff by display order). Then:

    - progressive: no filter, 0 points;
    - telecined: 'fieldmatch,decimate', output 24000/1001, 10 points;
    - interlaced: 'bwdif=mode=1:parity=0|1' at the field rate, 5 points;
      above 576 lines that needs level 4.2 (flag 'level-4.2'), unless
      max_level is '4.1', which gives 'bwdif=mode=0' at the frame rate;
    - mixed: as interlaced, plus flag 'cadence-mixed', 40 points (instead
      of 5). Unknown order gives parity=-1 (bwdif's automatic choice).

    Result: {'kind', 'order', 'filter', 'output_fps', 'level',
    'fps_multiplier'}; output_fps is a fraction string or None when the
    source rate is unknown.
    """
    summary = idet_summary or {"kind": "progressive", "order": None, "reason": "no idet run", "determined_frames": 0}
    kind = summary.get("kind", "progressive")
    order = summary.get("order")
    reason = "idet: " + summary.get("reason", "")
    field_order = video_stream.get("field_order")
    if kind == "progressive" and not summary.get("determined_frames") and field_order in INTERLACED_FIELD_ORDERS:
        kind = "interlaced"
        order = INTERLACED_FIELD_ORDERS[field_order]
        reason = "idet gave no evidence; container field_order '{}' says interlaced {}".format(field_order, order)

    source_fps = video_stream.get("fps_fraction")
    height = video_stream.get("height") or 0
    result = {"kind": kind, "order": order, "filter": None, "output_fps": source_fps, "level": "4.1",
              "fps_multiplier": 1.0}
    penalties = []
    flags = []
    if kind == "telecined":
        result.update(filter="fieldmatch,decimate", output_fps="24000/1001", fps_multiplier=0.8)
        penalties.append(("telecined", PENALTY_TELECINED, "3:2 telecine removed with fieldmatch,decimate"))
        reason += "; inverse telecine to 23.976p"
    elif kind in ("interlaced", "mixed"):
        parity = {"tff": 0, "bff": 1}.get(order, -1)
        if height > HD_HEIGHT_ABOVE and max_level == "4.1":
            result.update(filter="bwdif=mode=0:parity={}".format(parity))
            reason += "; HD interlaced, --max-level 4.1: frame-rate deinterlace"
        else:
            result.update(filter="bwdif=mode=1:parity={}".format(parity), output_fps=_double_rate(source_fps),
                          fps_multiplier=2.0)
            reason += "; field-rate deinterlace"
            if height > HD_HEIGHT_ABOVE:
                result["level"] = "4.2"
                flags.append("level-4.2")
                reason += " at level 4.2 ({} lines)".format(height)
        if kind == "mixed":
            flags.append("cadence-mixed")
            penalties.append(("cadence-mixed", PENALTY_CADENCE_MIXED, "cadence mixed or inconsistent across windows"))
        else:
            penalties.append(("interlaced", PENALTY_INTERLACED, "interlaced {} source deinterlaced".format(order)))
    return _decision(result, reason, penalties, flags, idet=summary)


# --- sync -----------------------------------------------------------------------------


def durations_from_packets(packets, sample_rate=None, initial_padding=0, count_gaps=True):
    """Timing of one stream from its packet list. Pure.

    Packets are timed by pts_time, or dts_time when pts is missing. 'start'
    is the first audible time: the earliest packet time plus the priming the
    decoder discards (the first packet's skip_samples side data, else the
    stream's initial_padding, both in samples at `sample_rate`): the AAC
    priming packet at -0.021 s in Matroska starts the sound at 0.000.
    'end' is the latest packet time plus its duration (packets without one
    get the median packet duration, else the median interval), less the
    last packet's discard_padding. 'packet_duration' is the median packet
    duration. 'gaps' counts jumps between successive packets larger than
    half a packet duration; 'gap_seconds' sums them. count_gaps=False
    (video: MPEG-PS leaves pts off some packets, and their dts are in decode
    order) reports None for the three gap values.

    Returns {'first_pts', 'start', 'end', 'duration', 'packets',
    'priming_s', 'packet_duration', 'gaps', 'gap_seconds', 'largest_gap_s'};
    the times are None when no packet carries a time.
    """
    timed = []
    known_durations = []
    for packet in packets:
        moment = _number(packet.get("pts_time"))
        if moment is None:
            moment = _number(packet.get("dts_time"))
        length = _number(packet.get("duration_time"))
        if length is not None and length > 0:
            known_durations.append(length)
        if moment is not None:
            timed.append((moment, length, packet))
    empty = {"first_pts": None, "start": None, "end": None, "duration": None, "packets": len(packets),
             "priming_s": 0.0, "packet_duration": None, "gaps": 0, "gap_seconds": 0.0, "largest_gap_s": 0.0}
    if not timed:
        return empty
    timed.sort(key=lambda entry: entry[0])
    typical = _median(known_durations)
    if typical is None:
        intervals = [later[0] - earlier[0] for earlier, later in zip(timed, timed[1:]) if later[0] > earlier[0]]
        typical = _median(intervals) or 0.0

    def side_value(packet, key):
        for entry in packet.get("side_data_list") or []:
            value = _number(entry.get(key))
            if value:
                return value
        return 0.0

    first_packet = packets[0] if packets else {}
    skip = side_value(first_packet, "skip_samples") or float(initial_padding or 0)
    priming = skip / float(sample_rate) if sample_rate and skip else 0.0
    first = timed[0][0]
    end = max(moment + (length if length is not None and length > 0 else typical) for moment, length, _ in timed)
    discard = side_value(timed[-1][2], "discard_padding")
    if sample_rate and discard:
        end -= discard / float(sample_rate)
    start = first + priming

    gaps = 0
    gap_seconds = 0.0
    largest = 0.0
    if not count_gaps:
        gaps = gap_seconds = largest = None
    elif typical > 0:
        for (moment, length, _), (later, _, _) in zip(timed, timed[1:]):
            step = later - (moment + (length if length is not None and length > 0 else typical))
            if step > AUDIO_GAP_PACKET_SHARE * typical:
                gaps += 1
                gap_seconds += step
                largest = max(largest, step)
    return {"first_pts": round(first, 6), "start": round(start, 6), "end": round(end, 6),
            "duration": round(end - start, 6), "packets": len(packets), "priming_s": round(priming, 6),
            "packet_duration": round(typical, 6), "gaps": gaps,
            "gap_seconds": None if gap_seconds is None else round(gap_seconds, 6),
            "largest_gap_s": None if largest is None else round(largest, 6)}


def measure_durations(tools, path, probe, progress=None):
    """Exact timing of the video stream and every audio stream from their packet lists (HANDOFF 4.4).

    Container durations are not used: Matroska streams report the format's
    duration, FLV and SWF none at all. Returns {'video': {...,'index'},
    'audio': [{..., 'index'}]} with durations_from_packets entries.
    """
    stream, _ = _progress_options(progress)
    absolute = os.path.abspath(os.fspath(path))
    raw = probe.get("raw")
    if raw is None:
        raw = ffprobe_json(tools, absolute)
    padding = {}
    for entry in raw.get("streams") or []:
        try:
            padding[entry.get("index")] = int(entry.get("initial_padding") or 0)
        except (TypeError, ValueError):
            padding[entry.get("index")] = 0
    targets = []
    if probe.get("video") is not None:
        targets.append(("video", probe["video"]))
    targets.extend(("audio", entry) for entry in probe.get("audio") or [])
    say("reading packet timing of {} stream(s) of '{}'".format(len(targets), _name(path)), stream)
    measured = {"video": None, "audio": []}
    for kind, entry in targets:
        index = entry["index"]
        packets = ffprobe_packets(tools, absolute, index, PACKET_ENTRIES)
        rate = entry.get("sample_rate") if kind == "audio" else None
        timing = durations_from_packets(packets, rate, padding.get(index, 0) if kind == "audio" else 0,
                                        count_gaps=(kind == "audio"))
        timing["index"] = index
        if kind == "video":
            measured["video"] = timing
        else:
            measured["audio"].append(timing)
    return measured


def decide_sync(probe, durations, audio_offset=None):
    """Audio/video sync decision (HANDOFF 4.4). Pure.

    Per audio stream: offset = audio start - video start (audible starts,
    see durations_from_packets); |offset| > 0.1 s flags 'sync-offset' and is
    recorded, not corrected (the real offset is kept in the output).
    `audio_offset` replaces the measured offset for every stream
    ('offset_source': 'option'; plan shifts by offset - measured_offset).

    Drift ratio = audio duration / video duration. Each stream's end is
    quantised to its own packets, so a difference no larger than one audio
    packet plus one video frame is not drift: the ratio is reported as 1.0
    with 'quantised': True (a 5 s H.264/AAC clip whose last AAC frame ends
    13 ms after the video would otherwise read 1.0027, and MP3 in AVI or
    FLV, 41 ms over on 5 s, 1.0083).
    Within 0.1 %: nothing. 0.1 % to 2 %: atempo=ratio, flag 'sync-drift',
    20 points, needs_reencode. Beyond 2 %: flag 'sync-drift-large', 100
    points (band hold), no correction. Timestamp gaps in an audio stream
    (packets missing) set needs_reencode so aresample=async=1 closes them,
    flag 'sync-gaps', no points.

    Result: [{'index', 'offset', 'measured_offset', 'offset_source', 'ratio',
    'atempo', 'needs_reencode', 'gaps'}] per audio stream.
    """
    durations = durations or {}
    video = durations.get("video") or {}
    entries = []
    penalties = []
    flags = []
    reasons = []
    by_index = {entry.get("index"): entry for entry in durations.get("audio") or []}
    for stream in probe.get("audio") or []:
        index = stream["index"]
        audio = by_index.get(index) or {}
        measured = None
        if audio.get("start") is not None and video.get("start") is not None:
            measured = round(audio["start"] - video["start"], 6)
        offset, source = measured, "measured"
        if audio_offset is not None:
            offset, source = float(audio_offset), "option"
        parts = []
        if source == "option":
            parts.append("offset {:+.3f} s from --audio-offset (measured {})".format(
                offset, "unknown" if measured is None else "{:+.3f} s".format(measured)))
        elif offset is None:
            parts.append("offset not measurable")
            flags.append("sync-unmeasured")
        else:
            parts.append("offset {:+.3f} s".format(offset))
            if abs(offset) > SYNC_OFFSET_S:
                flags.append("sync-offset")
                parts[-1] += " (over 0.1 s: recorded, kept as is)"

        ratio = None
        atempo = None
        needs_reencode = False
        quantised = False
        audio_length = audio.get("duration")
        video_length = video.get("duration")
        if audio_length and video_length and audio_length > 0 and video_length > 0:
            tolerance = (audio.get("packet_duration") or 0.0) + (video.get("packet_duration") or 0.0)
            if abs(audio_length - video_length) <= tolerance:
                ratio = 1.0
                quantised = True
                parts.append("durations {:.3f} s audio / {:.3f} s video differ by no more than one audio "
                             "packet plus one video frame".format(audio_length, video_length))
            else:
                ratio = round(audio_length / video_length, 6)
                drift = abs(ratio - 1.0)
                if drift <= DRIFT_NONE_BELOW:
                    parts.append("ratio {:.6f} within 0.1 %".format(ratio))
                elif drift <= DRIFT_CORRECT_UP_TO:
                    atempo = ratio
                    needs_reencode = True
                    flags.append("sync-drift")
                    penalties.append(("sync-drift", PENALTY_SYNC_DRIFT,
                                      "audio stream {} drift ratio {:.6f} corrected with atempo".format(index, ratio)))
                    parts.append("ratio {:.6f}: atempo={:.6f}".format(ratio, ratio))
                else:
                    flags.append("sync-drift-large")
                    penalties.append(("sync-drift-large", PENALTY_SYNC_DRIFT_LARGE,
                                      "audio stream {} drift ratio {:.6f} beyond 2 %, not corrected".format(
                                          index, ratio)))
                    parts.append("ratio {:.6f} beyond 2 %: not corrected, hold".format(ratio))
        else:
            parts.append("durations not measurable")
            if "sync-unmeasured" not in flags:
                flags.append("sync-unmeasured")
        gaps = audio.get("gaps") or 0
        if gaps:
            needs_reencode = True
            flags.append("sync-gaps")
            parts.append("{} timestamp gap(s), {:.3f} s in all".format(gaps, audio.get("gap_seconds") or 0.0))
        entries.append({"index": index, "offset": offset, "measured_offset": measured, "offset_source": source,
                        "ratio": ratio, "quantised": quantised, "atempo": atempo, "needs_reencode": needs_reencode,
                        "gaps": gaps})
        reasons.append("audio stream {}: {}".format(index, ", ".join(parts)))
    reason = "; ".join(reasons) if reasons else "no audio streams"
    return _decision(entries, reason, penalties, flags, [(0.0, None)], durations=durations)


# --- colour ---------------------------------------------------------------------------


def decide_colour(video_stream):
    """Colour tags (HANDOFF 4.5). Pure. Tag, never convert.

    Signalled colorspace/primaries/trc are kept; missing ones are inferred:
    SD (height <= 576) PAL family (576 or 288 lines, or 25/50 fps) gets
    space bt470bg, primaries bt470bg, trc bt709 (ffmpeg has no 'bt470bg'
    trc; BT.601-625's curve is BT.709's in practice); other SD smpte170m for
    all three; HD bt709. Range is always 'tv'.

    Result {'range', 'space', 'primaries', 'trc', 'source': 'signalled' |
    'inferred' | 'partly-signalled'}.
    """
    height = video_stream.get("height") or 0
    fps = video_stream.get("fps")
    if height and height <= SD_MAX_HEIGHT:
        pal = height in PAL_HEIGHTS or any(_near(fps, rate) for rate in PAL_FRAME_RATES)
        if pal:
            inferred = {"space": "bt470bg", "primaries": "bt470bg", "trc": "bt709"}
            why = "SD PAL family ({} lines, {} fps)".format(height, "unknown" if fps is None else "{:g}".format(fps))
        else:
            inferred = {"space": "smpte170m", "primaries": "smpte170m", "trc": "smpte170m"}
            why = "SD, not PAL ({} lines)".format(height)
    else:
        inferred = {"space": "bt709", "primaries": "bt709", "trc": "bt709"}
        why = "HD ({} lines)".format(height or "unknown")
    signalled = {"space": video_stream.get("color_space"), "primaries": video_stream.get("color_primaries"),
                 "trc": video_stream.get("color_transfer")}
    result = {"range": "tv"}
    kept = []
    for key in ("space", "primaries", "trc"):
        if signalled[key] is not None:
            result[key] = signalled[key]
            kept.append(key)
        else:
            result[key] = inferred[key]
    if len(kept) == 3:
        result["source"] = "signalled"
        reason = "explicit tags kept: {}/{}/{}".format(result["space"], result["primaries"], result["trc"])
    elif kept:
        result["source"] = "partly-signalled"
        reason = "kept signalled {}; inferred the rest from {}".format(", ".join(kept), why)
    else:
        result["source"] = "inferred"
        reason = "untagged; {} -> {}/{}/{}".format(why, result["space"], result["primaries"], result["trc"])
    return _decision(result, reason)


# --- pixel art ------------------------------------------------------------------------


def decide_pixelart(video_stream):
    """Integer upscale for palette/RLE content (HANDOFF 4.7). Pure.

    Palette class: pix_fmt pal8 or a palette/RLE codec. At most 300 lines:
    'scale=iw*N:ih*N:flags=neighbor' with N = 2, or 3 below 160 lines, tune
    'animation', flag 'pixelart-upscale'. Result {'filter', 'factor', 'tune'}.
    """
    codec = video_stream.get("codec")
    pix_fmt = video_stream.get("pix_fmt")
    height = video_stream.get("height") or 0
    palette = pix_fmt == "pal8" or codec in PALETTE_CODECS
    if not palette:
        return _decision({"filter": None, "factor": 1, "tune": None},
                         "codec '{}' ({}) is not palette/RLE class".format(codec, pix_fmt))
    if not height or height > PIXELART_MAX_HEIGHT:
        return _decision({"filter": None, "factor": 1, "tune": None},
                         "palette/RLE codec '{}' but {} lines is above 300: no upscale".format(codec, height))
    factor = 3 if height < PIXELART_TRIPLE_BELOW else 2
    return _decision({"filter": "scale=iw*{0}:ih*{0}:flags=neighbor".format(factor), "factor": factor,
                      "tune": "animation"},
                     "palette/RLE codec '{}' ({}) at {} lines: {}x nearest-neighbour upscale, tune animation".format(
                         codec, pix_fmt, height, factor),
                     flags=["pixelart-upscale"])


# --- audio ----------------------------------------------------------------------------


def aac_bitrate(channels):
    """'192k' for stereo: 96 kb/s per channel, capped at 448k; unknown channels count as 2."""
    count = channels if channels and channels > 0 else 2
    return "{}k".format(min(AUDIO_BITRATE_PER_CHANNEL_K * count, AUDIO_BITRATE_CAP_K))


def decide_audio(audio_stream, sync_entry, strict_aac=False):
    """Copy or AAC-LC re-encode for one audio stream (HANDOFF 2.2, 4.4). Pure.

    AAC-LC (profile 'LC' or not reported) and MP3 are copied unless MP3 with
    strict_aac, or the sync entry needs correction (needs_reencode: atempo or
    gap closing), which re-encodes with flag 'audio-reencoded-for-sync'.
    Everything else goes to AAC at 96 kb/s per channel (448k cap), channel
    layout and sample rate kept. Re-encoded streams get 'aresample=async=1'
    plus 'atempo=R' when the sync entry has one.

    Result {'index', 'action': 'copy'|'aac', 'bitrate', 'filters', 'channels',
    'channel_layout', 'sample_rate', 'language'}.
    """
    codec = audio_stream.get("codec")
    profile = audio_stream.get("profile")
    sync_entry = sync_entry or {}
    flags = []
    copy_eligible = (codec == "aac" and profile in (None, "LC")) or (codec == "mp3" and not strict_aac)
    if copy_eligible and not sync_entry.get("needs_reencode"):
        action = "copy"
        reason = "{} copied".format("AAC-LC" if codec == "aac" else "MP3")
    elif copy_eligible:
        action = "aac"
        flags.append("audio-reencoded-for-sync")
        why = []
        if sync_entry.get("atempo"):
            why.append("drift ratio {:.6f}".format(sync_entry["atempo"]))
        if sync_entry.get("gaps"):
            why.append("{} timestamp gap(s)".format(sync_entry["gaps"]))
        reason = "{} re-encoded for sync correction ({})".format(codec, ", ".join(why) or "needs correction")
    else:
        action = "aac"
        if codec == "mp3":
            reason = "MP3 re-encoded to AAC-LC (--audio-strict-aac)"
        elif codec == "aac":
            reason = "AAC profile '{}' is not LC: re-encoded to AAC-LC".format(profile)
        else:
            reason = "'{}' re-encoded to AAC-LC".format(codec)
    result = {"index": audio_stream.get("index"), "action": action, "bitrate": None, "filters": None,
              "channels": audio_stream.get("channels"), "channel_layout": audio_stream.get("channel_layout"),
              "sample_rate": audio_stream.get("sample_rate"), "language": audio_stream.get("language")}
    if action == "aac":
        result["bitrate"] = aac_bitrate(audio_stream.get("channels"))
        filters = ["aresample=async=1"]
        if sync_entry.get("atempo"):
            filters.append("atempo={:.6f}".format(sync_entry["atempo"]).rstrip("0").rstrip("."))
        result["filters"] = ",".join(filters)
        reason += " at {}".format(result["bitrate"])
    return _decision(result, reason, flags=flags)


# --- subtitles ------------------------------------------------------------------------


def decide_subtitles(subtitle_streams):
    """Subtitle handling (HANDOFF 2.3). Pure.

    Text codecs become mov_text in the MP4; ASS/SSA also get an '.srt'
    sidecar (styling is lost in mov_text). dvd_subtitle goes to a
    '.subs.mkv' sidecar, PGS to '.sup', flag 'subs-sidecar'. A codec that is
    neither is listed as 'unsupported' with flag 'subs-unsupported', never
    dropped silently. Result: [{'index', 'codec', 'action', 'sidecar',
    'language'}].
    """
    entries = []
    flags = []
    reasons = []
    for stream in subtitle_streams or []:
        codec = stream.get("codec")
        entry = {"index": stream.get("index"), "codec": codec, "action": None, "sidecar": None,
                 "language": stream.get("language")}
        if codec in TEXT_SUBTITLE_CODECS:
            entry["action"] = "mov_text"
            if codec in STYLED_SUBTITLE_CODECS:
                entry["sidecar"] = ".srt"
                reasons.append("stream {} {}: mov_text plus .srt sidecar (styling lost)".format(entry["index"], codec))
            else:
                reasons.append("stream {} {}: mov_text".format(entry["index"], codec))
        elif codec in BITMAP_SUBTITLE_SIDECARS:
            entry["action"] = "sidecar"
            entry["sidecar"] = BITMAP_SUBTITLE_SIDECARS[codec]
            flags.append("subs-sidecar")
            reasons.append("stream {} {}: bitmap, {} sidecar".format(entry["index"], codec, entry["sidecar"]))
        else:
            entry["action"] = "unsupported"
            flags.append("subs-unsupported")
            reasons.append("stream {} '{}': no MP4 or sidecar route".format(entry["index"], codec))
        entries.append(entry)
    return _decision(entries, "; ".join(reasons) if reasons else "no subtitle streams", flags=flags)


# --- remux eligibility and keyframes (HANDOFF 3) -------------------------------------


def remux_eligible(video_stream, cadence_decision, options=None):
    """(eligible, reason): H.264 Baseline/Main/High, level <= 4.1, yuv420p 8-bit, progressive cadence. Pure.

    The cadence decision decides "not interlaced": field_order None means
    unknown, not progressive. options.reencode_video forces False.
    """
    if getattr(options, "reencode_video", False):
        return (False, "--reencode-video given")
    codec = video_stream.get("codec")
    if codec != "h264":
        return (False, "video codec '{}' is not H.264".format(codec))
    profile = video_stream.get("profile")
    if profile not in REMUX_PROFILES:
        return (False, "H.264 profile '{}' is outside Baseline/Main/High".format(profile))
    level = video_stream.get("level")
    if level is None:
        return (False, "H.264 level unknown")
    if level > REMUX_MAX_LEVEL:
        return (False, "H.264 level {} is above 4.1".format(video_stream.get("level_text") or level))
    if video_stream.get("pix_fmt") != "yuv420p":
        return (False, "pixel format '{}' is not yuv420p".format(video_stream.get("pix_fmt")))
    bits = video_stream.get("bits_per_raw_sample")
    if bits not in (None, 8):
        return (False, "{}-bit video".format(bits))
    kind = ((cadence_decision or {}).get("result") or {}).get("kind")
    if kind != "progressive":
        return (False, "cadence is {}, not progressive".format(kind or "unknown"))
    return (True, "H.264 {} level {}, yuv420p, progressive".format(
        profile, video_stream.get("level_text") or level))


def decide_keyframes(spacing):
    """Keyframe-spacing decision from probe.keyframe_spacing output. Pure. Flag 'long-gop' above 5 s."""
    gap = spacing.get("max_gap_s")
    flags = []
    if gap is not None and gap > LONG_GOP_S:
        flags.append("long-gop")
        reason = "longest keyframe gap {:.3f} s, over 5 s".format(gap)
    elif gap is None:
        reason = "no keyframe gap measurable ({} keyframes)".format(spacing.get("keyframes"))
    else:
        reason = "longest keyframe gap {:.3f} s".format(gap)
    return _decision(spacing, reason, flags=flags, samples=[(0.0, None)])


# --- orchestration --------------------------------------------------------------------


def predicted_score(penalties):
    """100 minus the summed points, floored at 0."""
    return max(0, 100 - sum(entry["points"] for entry in penalties))


def analyze(tools, path, probe, options=None, progress=None):
    """Run the whole analysis of one file and return the analysis dict (HANDOFF 4).

    `probe` is probe.probe_file output for `path` with a video stream (plan
    quarantines the rest first). `options` may carry samples, full_analysis,
    assume_dar, audio_offset, audio_strict_aac, max_level, reencode_video.
    `progress` is None or an object with 'stream' and 'quiet' (the CLI args).
    Raises ValueError when the probe has no video stream and FfmpegError when
    an ffmpeg pass fails.
    """
    video = probe.get("video")
    if video is None:
        raise ValueError("'{}' has no video stream to analyse".format(_name(path)))
    samples = getattr(options, "samples", None) or DEFAULT_SAMPLES
    full = bool(getattr(options, "full_analysis", False))
    assume_dar = getattr(options, "assume_dar", None)
    audio_offset = getattr(options, "audio_offset", None)
    strict_aac = bool(getattr(options, "audio_strict_aac", False))
    max_level = getattr(options, "max_level", None) or "4.2"
    duration = (probe.get("format") or {}).get("duration")
    windows = sample_windows(duration, samples, WINDOW_SECONDS, full)

    decode = decide_decode(decode_run(tools, path, progress, duration))

    if needs_cropdetect(video):
        crop_windows = cropdetect_run(tools, path, windows, progress)
        crop = aggregate_crop(crop_windows, video.get("width"), video.get("height"))
        aspect = decide_aspect(video, crop, assume_dar)
        aspect["samples"] = [list(window) for window in windows]
        aspect["crop_windows"] = crop_windows
    else:
        aspect = decide_aspect(video, None, assume_dar)
        aspect["crop_windows"] = None

    idet_windows = idet_run(tools, path, windows, progress)
    cadence = decide_cadence(aggregate_idet(idet_windows, video.get("fps")), video, max_level)
    cadence["samples"] = [list(window) for window in windows]

    durations = measure_durations(tools, path, probe, progress)
    sync = decide_sync(probe, durations, audio_offset)
    sync_by_index = {entry["index"]: entry for entry in sync["result"]}

    colour = decide_colour(video)
    pixelart = decide_pixelart(video)
    audio = [decide_audio(stream, sync_by_index.get(stream["index"]), strict_aac) for stream in probe.get("audio") or []]
    subtitles = decide_subtitles(probe.get("subtitles") or [])

    eligible, eligible_reason = remux_eligible(video, cadence, options)
    keyframes = None
    if eligible:
        stream, quiet = _progress_options(progress)
        say("measuring keyframe spacing of '{}'".format(_name(path)), stream)
        with Progress("keyframes", total=None, unit=None, stream=stream, quiet=quiet) as live:
            keyframes = decide_keyframes(keyframe_spacing(tools, path, video["index"], live))

    decisions = [decode, aspect, cadence, sync, colour, pixelart] + audio + [subtitles]
    if keyframes is not None:
        decisions.append(keyframes)
    flags = _unique(flag for decision in decisions for flag in decision["flags"])
    penalties = [entry for decision in decisions for entry in decision["penalties"]]
    return {
        "version": VERSION,
        "windows": [list(window) for window in windows],
        "decode": decode,
        "aspect": aspect,
        "cadence": cadence,
        "sync": sync,
        "colour": colour,
        "pixelart": pixelart,
        "audio": audio,
        "subtitles": subtitles,
        "keyframes": keyframes,
        "remux": {"eligible": eligible, "reason": eligible_reason},
        "flags": flags,
        "penalties": penalties,
        "predicted_score": predicted_score(penalties),
    }

# End of File #
