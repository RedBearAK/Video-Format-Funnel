"""plan: probe and analyse each source, decide remux, re-encode or quarantine, build the exact ffmpeg argv.

HANDOFF.md sections 2, 3, 4.6 to 4.8, 5, 6.2, 6.7 and 6.10. `plan` writes the
per-file record (one JSON file per source under the folder's '.vidfunnel/')
holding the probe, the analysis, the decision with its reason and the argv
that `convert` will run; it never writes media.

What the record carries for the later stages:

- status 'planned' (action 'remux' or 'reencode'), 'quarantined' (with
  'quarantine_reason': 'swf-vector', 'no-video', 'unsupported',
  'decode-failed' or 'output-collision') or 'failed' (an ffmpeg or file
  error, with the message and stderr under stages.plan.error/stderr);
- source.sha256: the full SHA-256 of the input bytes (for a VOB group, of
  the members concatenated in order; for a DVD title read through the
  dvdvideo demuxer, of the disc's VIDEO_TS.IFO), whose first 16 hex digits
  are the identity tag's 'src';
- stages.plan.argv: ffmpeg argv, argv[0] the ffmpeg path, every path
  absolute, ending in the output path ('-f mp4' is explicit, so convert may
  substitute its '.part' name for the last element);
- stages.plan.ffmpeg: {'path', 'version', 'configuration_sha256'} of the
  ffmpeg the plan was made with (vidfunnel.ffmpeg.plan_identity), so convert
  refuses to run the argv with a different build found at the same path;
- stages.plan.sidecars: [{'path', 'argv', 'kind'}], one argv per subtitle
  sidecar, each ending in its own output path;
- stages.plan.size_class ({'class', 'bits_per_pixel'}) and
  stages.plan.blockiness ({'score'}), an informational copy of the
  analysis values (convert --refit reads stages.analyze, not these);
- output.name and output.interim_name ('<stem>.vidfunnel.mp4' when the
  source is itself '<stem>.mp4', and for every file source planned with
  --mark-outputs), output.sidecars (file names, final);
- stages.plan.mark_outputs: True when planned with --mark-outputs. Then the
  sidecars are written under interim names too ('<stem>.vidfunnel.srt'),
  listed in output.interim_sidecars beside output.sidecars (same order),
  and retire renames them with the output. DVD titles keep their final
  names, since retire does not retire a DVD folder.

Two sources whose outputs would share a name are both quarantined with
'output-collision' (HANDOFF 2.4), whether they are planned in the same run
or not: the folder's existing records are checked too. A record of another
source that is still 'planned' for that name is quarantined as well (its
stages.plan gains 'collision' and 'collision_reason'); one that is already
'converted' or 'verified' is left as it is and only the new source is
quarantined, with a reason naming the existing output.

Inputs that analyze() cannot open by path (a VOB group through the concat
protocol, a DVD title through the dvdvideo demuxer) are analysed by
analyze_input(), which runs the same passes with an input-argument list and
returns the same dict shape, built from analyze's own pure rules.
"""

VERSION = "20261109"

import hashlib
import json
import os
import re
import sys
import threading
import types
from pathlib import Path

import vidfunnel
from vidfunnel.analyze import (
    DEFAULT_SAMPLES,
    WINDOW_SECONDS,
    analyze,
    decide_audio,
    decide_colour,
    decide_decode,
    decide_keyframes,
    decide_pixelart,
    decide_size_class,
    decide_subtitles,
    decide_sync,
    durations_from_packets,
    has_blockdetect,
    needs_cropdetect,
    predicted_score,
    remux_eligible,
    sample_windows,
    window_decisions,
    window_pass,
    PACKET_ENTRIES,
)
from vidfunnel.ffmpeg import DecodeResult, FfmpegError, classify_messages, ffprobe_json, plan_identity
from vidfunnel.ffmpeg import capabilities as ffmpeg_capabilities
from vidfunnel.ffmpeg import run as run_tool
from vidfunnel.interrupt import check as check_interrupted
from vidfunnel.interrupt import drain_after_item, drain_before_next, run_pool
from vidfunnel.probe import (
    classify,
    decodable_codecs,
    is_dvd_structure,
    normalise_probe,
    probe_file,
    spacing_from_packets,
    vob_group,
)
from vidfunnel.progress import Progress, StageSummary, ffmpeg_progress_callback, format_bytes, item_tag, say, tagged
from vidfunnel.record import (
    FOLDER_FILE,
    RECORD_DIR,
    RECORD_SUFFIX,
    FolderLock,
    LockHeld,
    RecordError,
    add_flag,
    add_penalty,
    add_stage,
    cheap_fingerprint,
    fingerprint_matches,
    load_folder,
    load_record,
    new_record,
    now_iso,
    record_dir,
    save_folder,
    save_record,
    set_status,
)

SOURCE_EXTENSIONS = {
    "avi", "flv", "swf", "wmv", "asf", "mpg", "mpeg", "mpe", "m2v", "vob", "mov", "qt", "mkv", "webm", "mp4",
    "m4v", "3gp", "ts", "m2ts", "mts", "rm", "rmvb", "fli", "flc", "ogm", "ogv", "divx", "dv", "mxf",
}
PROFILE = "h264-high41-fmp4"
PLAIN_PROFILE = "h264-high41-mp4"
IDENTITY_KEY = "vidfunnel"
OUTPUT_EXTENSIONS = ("mp4", "m4v")
INTERIM_INFIX = ".vidfunnel"
RETIRED_DIR = ".vidfunnel-retired"
PART_SUFFIX = ".part"
SUBS_SIDECAR_SUFFIX = ".subs.mkv"
CLAIMING_STATUSES = ("planned", "converted", "verified")

FMP4_MOVFLAGS = "+frag_keyframe+empty_moov+default_base_moof+use_metadata_tags"
PLAIN_MOVFLAGS = "+faststart+use_metadata_tags"
MAX_LEVELS = ("4.1", "4.2")
DARS = ("4:3", "16:9")

HASH_PROGRESS_BYTES = 32 * 1024 * 1024
DVD_TITLE_CAP = 99
PENALTY_VOB_NO_IFO = 10
PENALTY_DVD_NO_DEMUXER = 10
AUDIO_SHIFT_EPSILON_S = 0.0005

OUTCOMES = ("remux", "re-encode", "quarantine", "skipped", "failed")
OUTCOME_OF_ACTION = {"remux": "remux", "reencode": "re-encode", "quarantine": "quarantine"}

# ITU-T H.273 code points for h264_metadata, by ffmpeg's names.
H273_PRIMARIES = {"bt709": 1, "bt470m": 4, "bt470bg": 5, "smpte170m": 6, "smpte240m": 7, "film": 8, "bt2020": 9,
                  "smpte428": 10, "smpte431": 11, "smpte432": 12, "jedec-p22": 22, "ebu3213": 22}
H273_TRANSFER = {"bt709": 1, "bt470m": 4, "gamma22": 4, "bt470bg": 5, "gamma28": 5, "smpte170m": 6,
                 "smpte240m": 7, "linear": 8, "log100": 9, "log316": 10, "iec61966-2-4": 11, "bt1361e": 12,
                 "iec61966-2-1": 13, "bt2020-10": 14, "bt2020-12": 15, "smpte2084": 16, "smpte428": 17,
                 "arib-std-b67": 18}
H273_MATRIX = {"gbr": 0, "bt709": 1, "fcc": 4, "bt470bg": 5, "smpte170m": 6, "smpte240m": 7, "ycgco": 8,
               "bt2020nc": 9, "bt2020c": 10, "smpte2085": 11, "chroma-derived-nc": 12, "chroma-derived-c": 13,
               "ictcp": 14}

PLAN_OPTION_NAMES = ("samples", "full_analysis", "assume_dar", "audio_offset", "audio_strict_aac", "reencode_video",
                     "max_level", "plain_mp4", "par2", "replan", "mark_outputs")

_TITLE_SET_VOB = re.compile(r"^(VTS_\d\d)_[1-9]\.VOB$", re.IGNORECASE)

_identity_cache = {}


class PlanError(Exception):
    """A source that plan cannot express as an ffmpeg command (e.g. '|' in a concatenated VOB path)."""


# --- options --------------------------------------------------------------------------


def make_options(**values):
    """The plan options as a namespace, defaults filled in (what run() builds from the CLI args)."""
    defaults = dict(samples=None, full_analysis=False, assume_dar=None, audio_offset=None, audio_strict_aac=False,
                    reencode_video=False, max_level="4.2", plain_mp4=False, par2=None, replan=False, jobs=1,
                    mark_outputs=False)
    defaults.update(values)
    return types.SimpleNamespace(**defaults)


def options_from_args(args):
    """make_options from the CLI args; 'jobs' is how the run is done, not a decision, so it is not recorded."""
    values = {name: getattr(args, name) for name in PLAN_OPTION_NAMES if hasattr(args, name)}
    values["jobs"] = getattr(args, "jobs", 1) or 1
    return make_options(**values)


def _options_dict(options):
    return {name: getattr(options, name, None) for name in PLAN_OPTION_NAMES}


def _progress_options(progress):
    if progress is None:
        return None, False
    return getattr(progress, "stream", None), bool(getattr(progress, "quiet", False))


# --- finding sources ------------------------------------------------------------------


def _extension(path):
    return Path(path).suffix.lower().lstrip(".")


_identity_lock = threading.Lock()


def read_identity(tools, path):
    """The parsed 'vidfunnel' format tag of a file, or None. Cached per (path, size, mtime) for the run.

    None when the file has no such tag, the tag is not a JSON object naming
    tool 'vidfunnel', or ffprobe cannot read the file (then it is not ours).
    """
    absolute = os.path.abspath(os.fspath(path))
    try:
        status = os.stat(absolute)
    except OSError:
        return None
    key = (absolute, status.st_size, status.st_mtime_ns)
    with _identity_lock:
        if key in _identity_cache:
            return _identity_cache[key]
    tag = None
    try:
        raw = ffprobe_json(tools, absolute)
    except FfmpegError:
        raw = {}
    for name, value in ((raw.get("format") or {}).get("tags") or {}).items():
        if name.lower() == IDENTITY_KEY:
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and parsed.get("tool") == IDENTITY_KEY:
                tag = parsed
            break
    with _identity_lock:
        _identity_cache[key] = tag
    return tag


def _skipped_name(name):
    return name.startswith(".") or name.endswith(PART_SUFFIX)


def _source_candidate(path, tools, seen_groups):
    """The source to yield for one file found by the walk (or given), or None to skip it."""
    if path.name.lower().endswith(SUBS_SIDECAR_SUFFIX):
        return None
    kind = is_dvd_structure(path)
    if kind == "vob_menu":
        return None
    if kind == "vob":
        match = _TITLE_SET_VOB.match(path.name)
        key = (str(path.parent), match.group(1).upper())
        if key in seen_groups:
            return None
        seen_groups.add(key)
        return Path(vob_group(path)[0])
    if _extension(path) in OUTPUT_EXTENSIONS and tools is not None and read_identity(tools, path) is not None:
        return None
    return path


def iter_sources(paths, tools=None):
    """Yield the sources under each PATH as absolute Paths, sorted within each folder.

    A directory is walked: files with a SOURCE_EXTENSIONS extension
    (case-insensitive), skipping hidden files and folders (so '.vidfunnel/'
    and '.vidfunnel-retired/'), '.part' files, our own 'NAME.subs.mkv'
    subtitle sidecars, menu VOBs and any .mp4/.m4v that carries our
    identity tag (read with ffprobe when `tools` is given).
    A title-set VOB stands for its group (VTS_xx_1.VOB is yielded once for
    all VTS_xx_N.VOB of the folder). A DVD tree (a VIDEO_TS folder, or one
    holding VIDEO_TS.IFO) is yielded as the folder itself and not entered;
    plan expands it into titles or title sets. A file PATH is taken as is,
    whatever its extension, except that a VOB stands for its group, an IFO
    for its DVD folder, and one of our own outputs or '.subs.mkv' sidecars
    is skipped.
    """
    seen_groups = set()
    seen = set()

    def emit(candidate):
        if candidate is not None and str(candidate) not in seen:
            seen.add(str(candidate))
            return True
        return False

    for given in paths:
        root = Path(os.path.abspath(os.fspath(given)))
        if root.is_dir():
            if is_dvd_structure(root) == "video_ts":
                if emit(root):
                    yield root
                continue
            for current, dir_names, file_names in os.walk(str(root)):
                folder = Path(current)
                kept = []
                for name in sorted(dir_names):
                    if name.startswith(".") or name in (RECORD_DIR, RETIRED_DIR):
                        continue
                    if is_dvd_structure(folder / name) == "video_ts":
                        if emit(folder / name):
                            yield folder / name
                        continue
                    kept.append(name)
                dir_names[:] = kept
                for name in sorted(file_names):
                    if _skipped_name(name) or _extension(name) not in SOURCE_EXTENSIONS:
                        continue
                    candidate = _source_candidate(folder / name, tools, seen_groups)
                    if emit(candidate):
                        yield candidate
        elif is_dvd_structure(root) == "ifo":
            if emit(root.parent):
                yield root.parent
        else:
            candidate = _source_candidate(root, tools, seen_groups)
            if candidate is None and root.name.lower().endswith(SUBS_SIDECAR_SUFFIX):
                say("skipping '{}': a '{}' name is one of our subtitle sidecars".format(root.name,
                                                                                       SUBS_SIDECAR_SUFFIX))
            elif candidate is None and _extension(root) in OUTPUT_EXTENSIONS:
                say("skipping '{}': it carries the vidfunnel identity tag (one of our outputs)".format(root.name))
            if emit(candidate):
                yield candidate


# --- names and identity ---------------------------------------------------------------


def output_stem(source):
    """The stem the output is named after: 'VTS_01' for a title-set VOB group, else the file's stem."""
    source = Path(source)
    match = _TITLE_SET_VOB.match(source.name)
    if match:
        return match.group(1)
    return source.stem


def _same_vob_group(first, second):
    first, second = Path(first), Path(second)
    one = _TITLE_SET_VOB.match(first.name)
    other = _TITLE_SET_VOB.match(second.name)
    return bool(one and other and first.parent == second.parent and one.group(1).upper() == other.group(1).upper())


def _same_file(first, second):
    try:
        return os.path.samefile(str(first), str(second))
    except OSError:
        return os.path.abspath(str(first)) == os.path.abspath(str(second))


def output_names(source, sibling_sources, tools=None, source_sha256=None, stem=None, mark=False):
    """{'name', 'interim_name', 'collision'} for a source (HANDOFF 2.4).

    name: '<stem>.mp4' beside the source. interim_name: '<stem>.vidfunnel.mp4'
    when the name is the source's own (an .mp4 source) or `mark` is true
    (plan --mark-outputs), else None. With `mark` the final name is checked
    for a foreign file as well as the interim one, since retire renames the
    output to it.
    collision: 'same-stem' when another source in the folder would get the
    same output name (compared case-insensitively, as macOS and SMB shares
    do); 'foreign-output' when the file convert would write (the interim
    name when there is one) already exists, is not the source, and does not
    carry our identity tag with the source's 'src' (without `tools` or
    `source_sha256` the tag cannot be checked and any such file counts as
    foreign); else None. `stem` overrides the name's stem (DVD titles).
    Sources planned in earlier runs are not among `sibling_sources`;
    recorded_claims() finds their records.
    """
    source = Path(os.path.abspath(os.fspath(source)))
    stem = stem if stem is not None else output_stem(source)
    name = stem + ".mp4"
    interim_name = None
    if mark or source.name.casefold() == name.casefold():
        interim_name = stem + INTERIM_INFIX + ".mp4"
    collision = None
    for sibling in sibling_sources or []:
        sibling = Path(os.path.abspath(os.fspath(sibling)))
        if sibling.parent != source.parent or sibling == source or _same_vob_group(sibling, source):
            continue
        if output_stem(sibling).casefold() == stem.casefold():
            collision = "same-stem"
            break
    targets = [interim_name or name]
    if mark and interim_name is not None:
        targets.append(name)
    collision_name = None
    for target_name in targets:
        if collision is not None:
            break
        target = source.parent / target_name
        if target.exists() and not _same_file(target, source):
            tag = read_identity(tools, target) if tools is not None else None
            ours = bool(tag and source_sha256 and tag.get("src") == source_sha256[:16])
            if not ours:
                collision = "foreign-output"
                collision_name = target_name
    names = {"name": name, "interim_name": interim_name, "collision": collision}
    if collision_name is not None and collision_name != (interim_name or name):
        names["collision_name"] = collision_name
    return names


def _record_key_name(record):
    """The file name a record is keyed by (as encode.record_key builds it), or None."""
    source = record.get("source") or {}
    name = source.get("name")
    if not isinstance(name, str):
        return None
    if source.get("kind") == "dvd-title":
        try:
            return "{}_title{:02d}".format(name, int(source.get("dvd_title") or 0))
        except (TypeError, ValueError):
            return None
    return name


def recorded_claims(folder, name, own_key_name, stream=None):
    """[(key path, record)] of the folder's records of other sources that claim output `name`.

    A record claims the name when its status is 'planned', 'converted' or
    'verified' and its output.name equals `name` compared case-insensitively;
    the record keyed by `own_key_name` (this source's own) is left out. Only
    the folder's own '.vidfunnel/' is read. An unreadable record is reported
    on `stream` and passed over.
    """
    location = record_dir(folder)
    try:
        entries = sorted(os.listdir(str(location)))
    except FileNotFoundError:
        return []
    wanted = name.casefold()
    claims = []
    for entry in entries:
        if entry.startswith(".") or entry == FOLDER_FILE or not entry.endswith(RECORD_SUFFIX):
            continue
        key = Path(folder) / entry[:-len(RECORD_SUFFIX)]
        if key.name == own_key_name:
            continue
        try:
            record = load_record(key)
        except RecordError as error:
            say("warning: cannot check '{}' for an output-name collision: {}".format(entry, error), stream)
            continue
        if record is None or record.get("status") not in CLAIMING_STATUSES:
            continue
        claimed = (record.get("output") or {}).get("name")
        if isinstance(claimed, str) and claimed.casefold() == wanted and _record_key_name(record) != own_key_name:
            claims.append((key, record))
    return claims


def _claims_reason(claims, name):
    parts = ["'{}' ({})".format(_record_key_name(record) or key.name, record.get("status")) for key, record in claims]
    done = [record for _key, record in claims if record.get("status") != "planned"]
    if done:
        return ("'{}' is already the output of {}; a person picks one".format(name, ", ".join(parts)))
    return "another source in this folder, {}, would also be converted to '{}'; a person picks one".format(
        ", ".join(parts), name)


def _quarantine_claim(key, record, new_key_name, name, stream):
    """Quarantine another source's 'planned' record that a new source collides with (HANDOFF 2.4: both hold)."""
    reason = "'{}' would also be converted to '{}' (found when it was planned); a person picks one".format(
        new_key_name, name)
    record["action"] = "quarantine"
    record["quarantine_reason"] = "output-collision"
    add_flag(record, "output-collision")
    plan = (record.get("stages") or {}).get("plan")
    if isinstance(plan, dict):
        plan["collision"] = "same-stem"
        plan["collision_reason"] = reason
    set_status(record, "quarantined")
    save_record(key, record)
    say("quarantined '{}' too: {}".format(_record_key_name(record) or key.name, reason), stream)


def identity_tag(record_id, source_sha256, profile=PROFILE):
    """The identity tag JSON of HANDOFF 6.7, compact: id, tool, ver, src (16 hex), profile."""
    payload = [("id", record_id), ("tool", IDENTITY_KEY), ("ver", vidfunnel.VERSION),
               ("src", (source_sha256 or "")[:16]), ("profile", profile)]
    return json.dumps(dict(payload), separators=(",", ":"))


# --- decisions ------------------------------------------------------------------------


def crf_for_height(height):
    """18 at <= 576 lines, 19 at <= 720, 20 above (HANDOFF 2.1). Unknown height counts as SD (18)."""
    if not height or height <= 576:
        return 18
    if height <= 720:
        return 19
    return 20


def _decodable_output(analysis):
    """True when the idet pass decoded at least one frame (the decode evidence the analysis keeps)."""
    totals = ((analysis.get("cadence") or {}).get("idet") or {}).get("totals") or {}
    return sum(totals.get(key) or 0 for key in ("tff", "bff", "progressive", "undetermined")) > 0


def decide_action(probe, analysis, options, capabilities):
    """(action, reason): ('quarantine', code) or ('remux' | 'reencode', why).

    Quarantine codes: 'swf-vector', 'no-video', 'unsupported' (probe.classify,
    with the decodable codec set under capabilities['decodable'] when
    present) and 'decode-failed' (the decode check exited non-zero and the
    idet pass decoded no frame). Remux when analysis['remux']['eligible'] and
    not options.reencode_video; else re-encode.
    """
    decodable = (capabilities or {}).get("decodable")
    kind, _reason = classify(probe, decodable)
    if kind != "video":
        return ("quarantine", kind)
    if analysis is None:
        raise ValueError("a source with a video stream needs an analysis to decide its action")
    decode = (analysis.get("decode") or {}).get("result") or {}
    if decode.get("returncode") not in (0, None) and not _decodable_output(analysis):
        return ("quarantine", "decode-failed")
    remux = analysis.get("remux") or {}
    if getattr(options, "reencode_video", False):
        return ("reencode", "--reencode-video given")
    if remux.get("eligible"):
        return ("remux", remux.get("reason") or "remux eligible")
    return ("reencode", remux.get("reason") or "not remux eligible")


def plan_size_entries(analysis):
    """(size_class, blockiness) for stages.plan: {'class', 'bits_per_pixel'} and {'score'}; informational.

    Either is None when the analysis has no such entry (an analysis made
    before 20261107).
    """
    size = (analysis or {}).get("size_class")
    blocks = (analysis or {}).get("blockiness")
    size_entry = None if not isinstance(size, dict) else {"class": size.get("class"),
                                                          "bits_per_pixel": size.get("bits_per_pixel")}
    block_entry = None if not isinstance(blocks, dict) else {"score": blocks.get("score")}
    return size_entry, block_entry


def _sar_changed(analysis):
    aspect = (analysis.get("aspect") or {}).get("result") or {}
    return aspect.get("source") in ("bars", "assumed") and bool(aspect.get("sar"))


def video_filters(analysis, action):
    """The -vf chain for a re-encode, or None.

    Cadence filter, pixel-art scale, setsar when the aspect decision changed
    the SAR, then setparams with the colour decision. setparams is there
    because ffmpeg 7.1.5 lets the frames' unspecified primaries and transfer
    override the -color_primaries and -color_trc output options (tested
    2026-11-02: an MPEG-2 source re-encoded with them read back 'unknown';
    with setparams it reads back smpte170m); the flags stay in the argv too.
    """
    if action != "reencode":
        return None
    parts = []
    cadence = (analysis.get("cadence") or {}).get("result") or {}
    if cadence.get("filter"):
        parts.append(cadence["filter"])
    pixelart = (analysis.get("pixelart") or {}).get("result") or {}
    if pixelart.get("filter"):
        parts.append(pixelart["filter"])
    if _sar_changed(analysis):
        parts.append("setsar=" + analysis["aspect"]["result"]["sar"].replace(":", "/"))
    colour = (analysis.get("colour") or {}).get("result") or {}
    settings = ["range={}".format(colour.get("range") or "tv")]
    for name, key in (("color_primaries", "primaries"), ("color_trc", "trc"), ("colorspace", "space")):
        if colour.get(key):
            settings.append("{}={}".format(name, colour[key]))
    parts.append("setparams=" + ":".join(settings))
    return ",".join(parts)


def _fraction_value(text):
    if not text:
        return None
    numerator, _, denominator = str(text).partition("/")
    try:
        top = float(numerator)
        bottom = float(denominator) if denominator else 1.0
    except ValueError:
        return None
    if top <= 0 or bottom <= 0:
        return None
    return top / bottom


def output_fps(probe, analysis):
    """Output frame rate: the cadence output_fps, else source fps times the cadence multiplier; None if unknown."""
    cadence = (analysis.get("cadence") or {}).get("result") or {}
    value = _fraction_value(cadence.get("output_fps"))
    if value is not None:
        return value
    video = probe.get("video") or {}
    source = video.get("fps") or video.get("avg_fps")
    if not source:
        return None
    return source * float(cadence.get("fps_multiplier") or 1.0)


def _video_map(probe):
    video = probe.get("video") or {}
    for stream in probe.get("streams") or []:
        if stream.get("type") == "video":
            if stream.get("index") == video.get("index"):
                return "0:v:0"
            break
    return "0:{}".format(video.get("index"))


def _colour_metadata(colour):
    fields = []
    for key, name, table in (("primaries", "colour_primaries", H273_PRIMARIES),
                             ("trc", "transfer_characteristics", H273_TRANSFER),
                             ("space", "matrix_coefficients", H273_MATRIX)):
        value = colour.get(key)
        if value in table:
            fields.append("{}={}".format(name, table[value]))
    fields.append("video_full_range_flag=0")
    return fields


def input_arguments(source_inputs, dvd_title=None):
    """['-i', PATH], ['-i', 'concat:P1|P2'] for several, ['-f', 'dvdvideo', '-title', N, '-i', DIR] for a title."""
    inputs = [os.path.abspath(os.fspath(path)) for path in source_inputs]
    if not inputs:
        raise PlanError("no input given")
    if dvd_title is not None:
        return ["-f", "dvdvideo", "-title", str(dvd_title), "-i", inputs[0]]
    if len(inputs) == 1:
        return ["-i", inputs[0]]
    for path in inputs:
        if "|" in path:
            raise PlanError("'{}' contains '|', which the concat protocol cannot carry".format(path))
    return ["-i", "concat:" + "|".join(inputs)]


def _audio_shifts(analysis):
    """Per audio stream, the shift --audio-offset asks for (offset minus measured), or None when not given."""
    entries = (analysis.get("sync") or {}).get("result") or []
    if not any(entry.get("offset_source") == "option" for entry in entries):
        return None
    shifts = {}
    for entry in entries:
        if entry.get("offset_source") == "option":
            shifts[entry.get("index")] = float(entry.get("offset") or 0.0) - float(entry.get("measured_offset") or 0.0)
    if all(abs(value) < AUDIO_SHIFT_EPSILON_S for value in shifts.values()):
        return None
    return shifts


def build_argv(tools, source_inputs, output_path, probe, analysis, action, options, record_id, source_sha256,
               dvd_title=None):
    """The ffmpeg argv convert runs (HANDOFF 2 and 4). See the module docstring for its shape."""
    if action not in ("remux", "reencode"):
        raise ValueError("no ffmpeg command for action '{}'".format(action))
    if not source_sha256:
        raise ValueError("the identity tag needs the source SHA-256")
    inputs = input_arguments(source_inputs, dvd_title)
    argv = [tools.ffmpeg, "-hide_banner", "-nostdin", "-y", "-fflags", "+genpts"] + inputs
    audio_decisions = analysis.get("audio") or []
    shifts = _audio_shifts(analysis)
    audio_maps = []
    if shifts is None:
        audio_maps = ["-map", "0:a?"]
    else:
        for position, decision in enumerate(audio_decisions):
            shift = shifts.get(decision["result"].get("index"), 0.0)
            argv += ["-fflags", "+genpts", "-itsoffset", "{:.6f}".format(shift)] + inputs
            audio_maps += ["-map", "{}:a:{}".format(position + 1, position)]
    argv += ["-avoid_negative_ts", "make_zero", "-map_metadata", "0", "-map_chapters", "0",
             "-map", _video_map(probe)] + audio_maps
    text_subtitles = False
    for position, entry in enumerate((analysis.get("subtitles") or {}).get("result") or []):
        if entry.get("action") == "mov_text":
            argv += ["-map", "0:s:{}".format(position)]
            text_subtitles = True
    argv += ["-nostats", "-progress", "pipe:1"]

    aspect = (analysis.get("aspect") or {}).get("result") or {}
    colour = (analysis.get("colour") or {}).get("result") or {}
    if action == "remux":
        argv += ["-c:v", "copy"]
        fields = []
        if _sar_changed(analysis):
            fields.append("sample_aspect_ratio=" + aspect["sar"].replace(":", "/"))
        if colour.get("source") in ("inferred", "partly-signalled"):
            fields += _colour_metadata(colour)
        if fields:
            argv += ["-bsf:v", "h264_metadata=" + ":".join(fields)]
        if _sar_changed(analysis) and aspect.get("dar"):
            argv += ["-aspect", aspect["dar"]]
    else:
        filters = video_filters(analysis, action)
        if filters:
            argv += ["-vf", filters]
        cadence = (analysis.get("cadence") or {}).get("result") or {}
        video = probe.get("video") or {}
        argv += ["-c:v", "libx264", "-preset", "slower", "-crf", str(crf_for_height(video.get("height"))),
                 "-profile:v", "high", "-level", cadence.get("level") or "4.1", "-pix_fmt", "yuv420p"]
        rate = output_fps(probe, analysis)
        if rate:
            argv += ["-g", str(int(round(2 * rate))), "-keyint_min", str(int(round(rate)))]
        argv += ["-x264-params", "open-gop=0", "-force_key_frames", "expr:gte(t,n_forced*2)"]
        pixelart = (analysis.get("pixelart") or {}).get("result") or {}
        if pixelart.get("tune"):
            argv += ["-tune", pixelart["tune"]]
        argv += ["-color_range", colour.get("range") or "tv"]
        for option, key in (("-colorspace", "space"), ("-color_primaries", "primaries"), ("-color_trc", "trc")):
            if colour.get(key):
                argv += [option, colour[key]]
        if aspect.get("dar"):
            argv += ["-aspect", aspect["dar"]]

    for position, decision in enumerate(audio_decisions):
        result = decision["result"]
        if result.get("action") == "copy":
            argv += ["-c:a:{}".format(position), "copy"]
        else:
            argv += ["-c:a:{}".format(position), "aac", "-b:a:{}".format(position), result.get("bitrate") or "192k",
                     "-filter:a:{}".format(position), result.get("filters") or "aresample=async=1"]
    if text_subtitles:
        argv += ["-c:s", "mov_text"]
    profile = PLAIN_PROFILE if getattr(options, "plain_mp4", False) else PROFILE
    argv += ["-metadata", "{}={}".format(IDENTITY_KEY, identity_tag(record_id, source_sha256, profile))]
    argv += ["-movflags", PLAIN_MOVFLAGS if getattr(options, "plain_mp4", False) else FMP4_MOVFLAGS]
    argv += ["-f", "mp4", os.path.abspath(os.fspath(output_path))]
    return argv


def _sidecar_base(output_path):
    text = os.path.abspath(os.fspath(output_path))
    return text[:-4] if text.lower().endswith(".mp4") else text


def build_sidecar_argvs(tools, source_inputs, output_path, analysis, dvd_title=None):
    """One argv per subtitle sidecar (HANDOFF 2.3), named after `output_path` (the final output name).

    Every text stream that goes into the MP4 as mov_text (subrip, ASS/SSA,
    WebVTT, mov_text, text) -> 'NAME.srt' as well, since an external .srt is
    what Roku Media Player and most TV players read; PGS -> 'NAME.sup'; every
    dvd_subtitle stream into one 'NAME.subs.mkv'. With several streams of one
    kind the subtitle number goes in: 'NAME.2.srt'. A text stream counts by
    its action 'mov_text' as well as by sidecar '.srt', so an analysis saved
    before every text stream was marked for a sidecar still gets one.
    """
    inputs = input_arguments(source_inputs, dvd_title)
    base = _sidecar_base(output_path)
    head = [tools.ffmpeg, "-hide_banner", "-nostdin", "-y"] + inputs
    entries = list(enumerate((analysis.get("subtitles") or {}).get("result") or []))
    text_streams = [position for position, entry in entries
                    if entry.get("action") == "mov_text" or entry.get("sidecar") == ".srt"]
    pgs = [position for position, entry in entries if entry.get("sidecar") == ".sup"]
    vobsub = [position for position, entry in entries if entry.get("sidecar") == ".subs.mkv"]
    sidecars = []
    for position in text_streams:
        path = base + (".srt" if len(text_streams) == 1 else ".{}.srt".format(position))
        sidecars.append({"path": path, "kind": "srt",
                         "argv": head + ["-map", "0:s:{}".format(position), "-c:s", "srt", "-f", "srt", path]})
    for position in pgs:
        path = base + (".sup" if len(pgs) == 1 else ".{}.sup".format(position))
        sidecars.append({"path": path, "kind": "sup",
                         "argv": head + ["-map", "0:s:{}".format(position), "-c:s", "copy", "-f", "sup", path]})
    if vobsub:
        path = base + ".subs.mkv"
        maps = []
        for position in vobsub:
            maps += ["-map", "0:s:{}".format(position)]
        sidecars.append({"path": path, "kind": "subs.mkv",
                         "argv": head + maps + ["-c:s", "copy", "-f", "matroska", path]})
    return sidecars


# --- analysis of inputs given as ffmpeg arguments (VOB groups, DVD titles) ------------


def _ffprobe_json_input(tools, arguments, label):
    argv = [tools.ffprobe, "-v", "error", "-print_format", "json"] + arguments
    completed = run_tool(argv, check=False)
    if completed.returncode != 0:
        raise FfmpegError("ffprobe could not read '{}' (status {}): {}".format(
            label, completed.returncode, completed.stderr.strip()), argv, completed.returncode, completed.stderr)
    try:
        return json.loads(completed.stdout)
    except ValueError as error:
        raise FfmpegError("ffprobe printed invalid JSON for '{}': {}".format(label, error), argv,
                          completed.returncode, completed.stderr)


def probe_input(tools, input_args, label, size=None):
    """Probe dict (probe.normalise_probe) for an input given as ffmpeg input arguments."""
    raw = _ffprobe_json_input(tools, ["-show_format", "-show_streams", "-show_chapters"] + input_args, label)
    return normalise_probe(raw, label, size)


def _packets_input(tools, input_args, label, stream_index, entries):
    raw = _ffprobe_json_input(tools, ["-select_streams", str(stream_index), "-show_entries", "packet=" + entries]
                              + input_args, label)
    return raw.get("packets", [])


def _durations_input(tools, input_args, label, probe, progress):
    stream, _quiet = _progress_options(progress)
    padding = {}
    for entry in (probe.get("raw") or {}).get("streams") or []:
        try:
            padding[entry.get("index")] = int(entry.get("initial_padding") or 0)
        except (TypeError, ValueError):
            padding[entry.get("index")] = 0
    targets = [("video", probe["video"])] + [("audio", entry) for entry in probe.get("audio") or []]
    say("reading packet timing of {} stream(s) of '{}'".format(len(targets), label), stream)
    measured = {"video": None, "audio": []}
    for kind, entry in targets:
        index = entry["index"]
        packets = _packets_input(tools, input_args, label, index, PACKET_ENTRIES)
        rate = entry.get("sample_rate") if kind == "audio" else None
        timing = durations_from_packets(packets, rate, padding.get(index, 0) if kind == "audio" else 0,
                                        count_gaps=(kind == "audio"))
        timing["index"] = index
        if kind == "video":
            measured["video"] = timing
        else:
            measured["audio"].append(timing)
    return measured


def analyze_input(tools, input_args, label, probe, options=None, progress=None):
    """analyze() for an input given as ffmpeg input arguments; the same dict, plus 'input': label.

    The passes are those of vidfunnel.analyze.analyze (full decode check,
    vidfunnel.analyze.window_pass over the sample windows, packet timing,
    keyframe scan for remux candidates) with `input_args` in place of
    '-i PATH'; every decision comes from analyze's own decide_* rules. `probe` must keep its 'raw' JSON.
    """
    video = probe.get("video")
    if video is None:
        raise ValueError("'{}' has no video stream to analyse".format(label))
    samples = getattr(options, "samples", None) or DEFAULT_SAMPLES
    full = bool(getattr(options, "full_analysis", False))
    assume_dar = getattr(options, "assume_dar", None)
    audio_offset = getattr(options, "audio_offset", None)
    strict_aac = bool(getattr(options, "audio_strict_aac", False))
    max_level = getattr(options, "max_level", None) or "4.2"
    duration = (probe.get("format") or {}).get("duration")
    windows = sample_windows(duration, samples, WINDOW_SECONDS, full)
    stream, quiet = _progress_options(progress)

    say("decode check of '{}'".format(label), stream)
    argv = [tools.ffmpeg, "-v", "warning", "-nostats", "-progress", "pipe:1"] + input_args + [
        "-map", "0:v?", "-map", "0:a?", "-f", "null", "-"]
    with Progress("decode", total=duration, unit="s", stream=stream, quiet=quiet) as live:
        completed = run_tool(argv, progress=ffmpeg_progress_callback(live, duration), check=False)
    decode = decide_decode(DecodeResult(completed.returncode, classify_messages(completed.stderr), completed.stderr))

    found = window_pass(tools, input_args, windows, label, needs_cropdetect(video), has_blockdetect(tools), progress)
    aspect, cadence, blockiness = window_decisions(video, windows, found, assume_dar, max_level)

    sync = decide_sync(probe, _durations_input(tools, input_args, label, probe, progress), audio_offset)
    sync_by_index = {entry["index"]: entry for entry in sync["result"]}
    colour = decide_colour(video)
    pixelart = decide_pixelart(video)
    audio = [decide_audio(entry, sync_by_index.get(entry["index"]), strict_aac) for entry in probe.get("audio") or []]
    subtitles = decide_subtitles(probe.get("subtitles") or [])

    eligible, eligible_reason = remux_eligible(video, cadence, options)
    keyframes = None
    if eligible:
        say("measuring keyframe spacing of '{}'".format(label), stream)
        packets = _packets_input(tools, input_args, label, video["index"], "pts_time,dts_time,duration_time,flags")
        keyframes = decide_keyframes(spacing_from_packets(packets))

    decisions = [decode, aspect, cadence, sync, colour, pixelart] + audio + [subtitles]
    if keyframes is not None:
        decisions.append(keyframes)
    flags = []
    for decision in decisions:
        for flag in decision["flags"]:
            if flag not in flags:
                flags.append(flag)
    penalties = [entry for decision in decisions for entry in decision["penalties"]]
    return {
        "version": VERSION,
        "input": label,
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
        "size_class": decide_size_class(probe),
        "blockiness": blockiness,
        "flags": flags,
        "penalties": penalties,
        "predicted_score": predicted_score(penalties),
    }


# --- DVD structures (HANDOFF 4.6) -----------------------------------------------------


def _find_name(folder, wanted):
    try:
        for entry in sorted(os.listdir(str(folder))):
            if entry.upper() == wanted.upper():
                return Path(folder) / entry
    except OSError:
        return None
    return None


def dvd_titles(tools, directory, progress=None):
    """[(title number, probe)] for a DVD folder, probing '-f dvdvideo -title N' from 1 until it fails (cap 99)."""
    stream, _quiet = _progress_options(progress)
    say("enumerating DVD titles of '{}'".format(Path(directory).name), stream)
    titles = []
    for number in range(1, DVD_TITLE_CAP + 1):
        try:
            found = probe_input(tools, input_arguments([directory], number), "{} title {}".format(directory, number))
        except FfmpegError:
            break
        titles.append((number, found))
    return titles


def title_set_groups(directory):
    """The first VOB of every title set in a DVD folder (the concatenation path without the demuxer)."""
    firsts = []
    seen = set()
    for entry in sorted(os.listdir(str(directory))):
        match = _TITLE_SET_VOB.match(entry)
        if match and match.group(1).upper() not in seen:
            seen.add(match.group(1).upper())
            firsts.append(Path(vob_group(Path(directory) / entry)[0]))
    return firsts


# --- one file -------------------------------------------------------------------------


def _strip_raw(probe):
    return {key: value for key, value in probe.items() if key != "raw"}


def _sha256_paths(paths, label, progress):
    """Full SHA-256 of the files concatenated in order, with live progress above 32 MiB."""
    stream, quiet = _progress_options(progress)
    total = sum(os.path.getsize(str(path)) for path in paths)
    digest = hashlib.sha256()
    live = None
    if total > HASH_PROGRESS_BYTES:
        say("hashing '{}' ({})".format(label, format_bytes(total)), stream)
        live = Progress("sha256", total=total, unit="B", stream=stream, quiet=quiet)
    done = 0
    try:
        for path in paths:
            with open(str(path), "rb") as handle:
                while True:
                    check_interrupted()
                    block = handle.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                    done += len(block)
                    if live is not None:
                        live.update(done)
    finally:
        if live is not None:
            live.finish()
    return digest.hexdigest()


def _member_fingerprints(paths):
    return [{"name": Path(path).name, "fingerprint": cheap_fingerprint(path)} for path in paths]


def _unchanged(record, fingerprint, members):
    if not fingerprint_matches((record.get("source") or {}).get("fingerprint"), fingerprint):
        return False
    recorded = (record.get("source") or {}).get("members")
    if members is None and recorded is None:
        return True
    if members is None or recorded is None or len(members) != len(recorded):
        return False
    for current, previous in zip(members, recorded):
        if current["name"] != previous.get("name"):
            return False
        if not fingerprint_matches(previous.get("fingerprint"), current["fingerprint"]):
            return False
    return True


class _Work(object):
    """Where one planned item lives: record key path, inputs, hashed files, names."""

    def __init__(self, source, dvd_title=None):
        self.source = Path(os.path.abspath(os.fspath(source)))
        self.dvd_title = dvd_title
        self.stem = None
        self.members = None
        self.flag = None
        if dvd_title is not None:
            self.stem = "{}_title{:02d}".format(self.source.name, dvd_title)
            self.key = self.source.parent / self.stem
            self.inputs = [str(self.source)]
            index = _find_name(self.source, "VIDEO_TS.IFO")
            if index is None:
                raise PlanError("DVD folder '{}' has no VIDEO_TS.IFO to fingerprint".format(self.source))
            self.hashed = [index]
            self.label = "{} title {}".format(self.source.name, dvd_title)
        elif is_dvd_structure(self.source) == "vob":
            group = [Path(path) for path in vob_group(self.source)]
            self.key = group[0]
            self.inputs = [str(path) for path in group]
            self.hashed = group
            self.members = group
            self.label = "{} ({} VOB{})".format(output_stem(group[0]), len(group), "" if len(group) == 1 else "s")
            in_dvd_tree = is_dvd_structure(self.source.parent) == "video_ts"
            self.flag = "dvd-no-demuxer" if in_dvd_tree else "vob-no-ifo"
        else:
            self.key = self.source
            self.inputs = [str(self.source)]
            self.hashed = [self.source]
            self.label = self.source.name
        self.folder = self.key.parent


def _new_record_for(work):
    record = new_record(work.hashed[0], work.folder)
    if work.dvd_title is not None:
        record["source"].update(name=work.source.name, kind="dvd-title", dvd_title=work.dvd_title,
                                fingerprint_of=work.hashed[0].name, size=None)
    elif work.members is not None:
        record["source"].update(kind="vob-group", members=_member_fingerprints(work.members),
                                size=sum(os.path.getsize(str(path)) for path in work.members))
    else:
        record["source"]["kind"] = "file"
    return record


def _attach_history(record, existing, fingerprint, members):
    """Move the source's existing record into the new record's history (marked re-planned or source-changed)."""
    if existing is None:
        return
    history = list(existing.pop("history", None) or [])
    if _unchanged(existing, fingerprint, members):
        existing["replanned_at"] = now_iso()
    else:
        set_status(existing, "source-changed")
    history.append(existing)
    record["history"] = history


def _plan_one(tools, work, options, folder_sources, capabilities, progress, known_probe=None, record_lock=None):
    """(outcome, record) for one work item. Saves the record unless the item was skipped.

    `record_lock` (plan --jobs: the folder's threading.Lock) is held from the
    reread of this source's record through the collision check against the
    folder's other records to the last save; the probe, the hash, the
    analysis and the output-name check run before it is taken.
    """
    stream, _quiet = _progress_options(progress)
    held = []
    try:
        return _plan_one_locked(tools, work, options, folder_sources, capabilities, progress, known_probe,
                                record_lock, held, stream)
    finally:
        if held:
            record_lock.release()


def _plan_one_locked(tools, work, options, folder_sources, capabilities, progress, known_probe, record_lock, held,
                     stream):
    def take_lock():
        if record_lock is not None and not held:
            record_lock.acquire()
            held.append(True)

    existing = load_record(work.key)
    fingerprint = cheap_fingerprint(work.hashed[0])
    members = _member_fingerprints(work.members) if work.members is not None else None
    replan = bool(getattr(options, "replan", False))
    if existing is not None and _unchanged(existing, fingerprint, members) and not replan:
        take_lock()
        return "skipped", load_record(work.key) or existing

    record = _new_record_for(work)
    if existing is not None:
        if not _unchanged(existing, fingerprint, members):
            say("'{}' changed since record {} was planned; re-planning under a new record".format(
                work.label, existing.get("id")), stream)
        old_sha = (existing.get("source") or {}).get("sha256")
        if old_sha and _unchanged(existing, fingerprint, members):
            record["source"]["sha256"] = old_sha
    settled = []

    def settle():
        """Take the folder's record lock, then the history from this source's record as it is now on disk.

        With --jobs the items that share this output name run before this
        one on the same worker, so the reread sees what a sequential run sees;
        another item's collision check changes only a record's status, flags
        and plan, never the fingerprint or the hash used above.
        """
        if settled:
            return settled[0]
        take_lock()
        current = load_record(work.key) if existing is not None else None
        _attach_history(record, current, fingerprint, members)
        settled.append(current)
        return current

    plan = {"inputs": list(work.inputs), "profile": PLAIN_PROFILE if getattr(options, "plain_mp4", False) else PROFILE,
            "options": _options_dict(options), "par2": getattr(options, "par2", None),
            "plain_mp4": bool(getattr(options, "plain_mp4", False)),
            "mark_outputs": bool(getattr(options, "mark_outputs", False)), "dvd_title": work.dvd_title, "argv": None,
            "sidecars": [], "output": {"name": None, "interim_name": None}, "predicted_score": None,
            "keyframes": None, "size_class": None, "blockiness": None,
            "ffmpeg": plan_identity(tools.ffmpeg, capabilities)}
    input_args = input_arguments(work.inputs, work.dvd_title)
    try:
        if known_probe is not None:
            probe = known_probe
        elif work.dvd_title is None and len(work.inputs) == 1:
            probe = probe_file(tools, work.inputs[0])
        else:
            size = record["source"].get("size")
            probe = probe_input(tools, input_args, work.label, size)
        add_stage(record, "probe", _strip_raw(probe))

        kind, kind_reason = classify(probe, capabilities.get("decodable"))
        if record["source"]["sha256"] is None:
            record["source"]["sha256"] = _sha256_paths(work.hashed, work.label, progress)
        if kind != "video":
            plan.update(action="quarantine", reason=kind_reason, analysis="skipped: {}".format(kind))
            settle()
            return _finish_quarantine(work, record, plan, kind)

        if work.dvd_title is None and len(work.inputs) == 1:
            analysis = analyze(tools, work.inputs[0], probe, options, progress)
        else:
            analysis = analyze_input(tools, input_args, work.label, probe, options, progress)
        add_stage(record, "analyze", analysis)
        for flag in analysis.get("flags") or []:
            add_flag(record, flag)
        for entry in analysis.get("penalties") or []:
            add_penalty(record, entry["code"], entry["points"], entry["reason"])
        if work.flag == "vob-no-ifo":
            add_flag(record, "vob-no-ifo")
            add_penalty(record, "vob-no-ifo", PENALTY_VOB_NO_IFO, "loose VOB fragments concatenated without an IFO")
        elif work.flag == "dvd-no-demuxer":
            add_flag(record, "dvd-no-demuxer")
            add_penalty(record, "dvd-no-demuxer", PENALTY_DVD_NO_DEMUXER,
                        "DVD title set concatenated: this ffmpeg has no dvdvideo demuxer")
        plan["size_class"], plan["blockiness"] = plan_size_entries(analysis)
        if analysis.get("keyframes"):
            spacing = analysis["keyframes"].get("result") or {}
            plan["keyframes"] = {key: spacing.get(key) for key in ("keyframes", "max_gap_s", "mean_gap_s",
                                                                   "max_gap_frames")}

        action, reason = decide_action(probe, analysis, options, capabilities)
        if action == "quarantine":
            decode_reason = (analysis.get("decode") or {}).get("reason")
            plan.update(action="quarantine", reason="{}: {}".format(reason, decode_reason))
            settle()
            return _finish_quarantine(work, record, plan, reason)

        if work.dvd_title is not None:
            names = _dvd_title_names(work, tools, record["source"]["sha256"])
        else:
            names = output_names(work.key, folder_sources, tools, record["source"]["sha256"],
                                 mark=plan["mark_outputs"])
        plan["output"] = {"name": names["name"], "interim_name": names["interim_name"]}
        record["output"]["name"] = names["name"]
        record["output"]["interim_name"] = names["interim_name"]
        final_path = work.folder / names["name"]
        sidecars = build_sidecar_argvs(tools, work.inputs, final_path, analysis, work.dvd_title)
        ours = _own_sidecar_names(settle())
        sidecars, skipped_sidecars = _drop_foreign_text_sidecars(sidecars, ours)
        for name in skipped_sidecars:
            add_flag(record, "sidecar-exists")
            say("'{}': '{}' already exists and is not ours; not written (the subtitles are inside the MP4 "
                "as mov_text)".format(work.label, name), stream)
        plan["sidecars_skipped"] = skipped_sidecars
        record["output"]["sidecars"] = [Path(entry["path"]).name for entry in sidecars]
        final_sidecar_paths = [entry["path"] for entry in sidecars]
        if plan["mark_outputs"] and names["interim_name"]:
            # Written under interim names beside the interim output; retire renames them with it.
            sidecars = interim_sidecars(sidecars, final_path, work.folder / names["interim_name"])
            record["output"]["interim_sidecars"] = [Path(entry["path"]).name for entry in sidecars]
        collision = names["collision"]
        reason = None
        claims = recorded_claims(work.folder, names["name"], work.key.name, stream)
        if claims and collision != "same-stem":
            collision = "same-stem"
            reason = _claims_reason(claims, names["name"])
        written_paths = [entry["path"] for entry in sidecars]
        taken = [path for path in dict.fromkeys(final_sidecar_paths + written_paths)
                 if os.path.exists(path) and Path(path).name not in ours]
        if collision is None and taken:
            collision = "sidecar-exists"
        if collision is not None:
            plan.update(action="quarantine", collision=collision,
                        reason=reason or _collision_reason(collision, names, taken))
            outcome = _finish_quarantine(work, record, plan, "output-collision")
            for key, other in claims:
                if other.get("status") == "planned":
                    _quarantine_claim(key, other, work.key.name, names["name"], stream)
            return outcome

        output_path = work.folder / (names["interim_name"] or names["name"])
        plan["argv"] = build_argv(tools, work.inputs, output_path, probe, analysis, action, options, record["id"],
                                  record["source"]["sha256"], work.dvd_title)
        plan["sidecars"] = sidecars
        plan.update(action=action, reason=reason, collision=None,
                    predicted_score=predicted_score(record["penalties"]))
        record["action"] = action
        add_stage(record, "plan", plan)
        set_status(record, "planned")
        save_record(work.key, record)
        return OUTCOME_OF_ACTION[action], record
    except (FfmpegError, PlanError, OSError) as error:
        plan.update(action=None, error=str(error), stderr=getattr(error, "stderr", None),
                    failed_argv=getattr(error, "argv", None))
        settle()
        if "plan" not in record["stages"]:
            add_stage(record, "plan", plan)
        set_status(record, "failed")
        save_record(work.key, record)
        say("failed: '{}': {}".format(work.label, error), stream)
        return "failed", record


def _dvd_title_names(work, tools, sha256):
    """Output names for a DVD title: '<dirname>_titleNN.mp4' beside the DVD folder."""
    name = work.stem + ".mp4"
    collision = None
    target = work.folder / name
    if target.exists():
        tag = read_identity(tools, target)
        if not (tag and tag.get("src") == sha256[:16]):
            collision = "foreign-output"
    return {"name": name, "interim_name": None, "collision": collision}


def _own_sidecar_names(existing):
    """Names of the sidecars this source's earlier records wrote (the record and its history).

    A file with one of these names beside the source is ours to overwrite,
    so a `plan --replan` after a convert does not trip over its own .srt.
    The interim names of a record planned with --mark-outputs count too.
    """
    names = set()
    for record in [existing] + list((existing or {}).get("history") or []):
        output = (record or {}).get("output") or {}
        for name in list(output.get("sidecars") or []) + list(output.get("interim_sidecars") or []):
            names.add(name)
    return names


def interim_sidecars(sidecars, final_path, interim_path):
    """The sidecar entries renamed for an interim output: 'NAME.srt' -> 'NAME.vidfunnel.srt'. Pure.

    Each entry's path and the last element of its argv (the same path) get
    the interim output's base in place of the final output's base; the
    kind and the rest of the argv stay as they are.
    """
    final_base = _sidecar_base(final_path)
    interim_base = _sidecar_base(interim_path)
    renamed = []
    for entry in sidecars:
        path = entry["path"]
        if not path.startswith(final_base):
            raise PlanError("sidecar '{}' is not named after the output '{}'".format(path, final_path))
        new_path = interim_base + path[len(final_base):]
        argv = list(entry["argv"])
        if argv and argv[-1] == path:
            argv[-1] = new_path
        renamed.append(dict(entry, path=new_path, argv=argv))
    return renamed


def _drop_foreign_text_sidecars(sidecars, ours):
    """Leave a user's own .srt alone: (sidecars to write, names of text sidecars skipped).

    A text sidecar whose path exists and is not ours is skipped, since the
    same subtitles go into the MP4 as mov_text and nothing is lost. Bitmap
    sidecars (.sup, .subs.mkv) are the only copy of their stream, so an
    existing foreign file there stays a collision for the caller to report.
    """
    kept = []
    skipped = []
    for entry in sidecars:
        name = Path(entry["path"]).name
        if entry.get("kind") == "srt" and os.path.exists(entry["path"]) and name not in ours:
            skipped.append(name)
        else:
            kept.append(entry)
    return kept, skipped


def _collision_reason(collision, names, taken):
    if collision == "same-stem":
        return "another source in this folder would also be converted to '{}'; a person picks one".format(
            names["name"])
    if collision == "foreign-output":
        return "'{}' already exists and is not ours (no vidfunnel tag with this source's src)".format(
            names.get("collision_name") or names["interim_name"] or names["name"])
    return "sidecar(s) already exist: {}".format(", ".join("'{}'".format(Path(path).name) for path in taken))


def _finish_quarantine(work, record, plan, reason_code):
    record["action"] = "quarantine"
    record["quarantine_reason"] = reason_code
    if reason_code == "output-collision":
        add_flag(record, "output-collision")
    plan.setdefault("collision", None)
    add_stage(record, "plan", plan)
    set_status(record, "quarantined")
    save_record(work.key, record)
    return "quarantine", record


def plan_file(tools, source, options, folder_sources, capabilities, progress=None, dvd_title=None,
              known_probe=None):
    """Plan one source (a file, the first VOB of a group, or a DVD folder with dvd_title) and return its record.

    The record is saved under the folder's '.vidfunnel/'. A source whose
    record's cheap fingerprint still matches is skipped (its record returned
    unchanged) unless options.replan. The caller holds the FolderLock.
    """
    work = _Work(source, dvd_title)
    _outcome, record = _plan_one(tools, work, options, folder_sources, capabilities, progress, known_probe)
    return record


# --- many files -----------------------------------------------------------------------


def _expand(tools, sources, capabilities, progress):
    """[(work, known_probe)] from iter_sources output: DVD folders become titles or title-set groups."""
    items = []
    for source in sources:
        if source.is_dir():
            titles = []
            if "dvdvideo" in capabilities.get("demuxers", set()):
                titles = dvd_titles(tools, source, progress)
            if titles:
                for number, found in titles:
                    items.append((_Work(source, number), found))
            else:
                for first in title_set_groups(source):
                    items.append((_Work(first), None))
        else:
            items.append((_Work(source), None))
    return items


def _table_row(outcome, record):
    plan = (record.get("stages") or {}).get("plan") or {}
    score = plan.get("predicted_score")
    size = (record.get("source") or {}).get("size")
    action = record.get("action") or "-"
    if outcome == "skipped":
        action = "skip ({})".format(record.get("status"))
    elif record.get("quarantine_reason"):
        action = "quarantine ({})".format(record["quarantine_reason"])
    return "  {:<34} {:>5} {:>10}  {}  {}".format(
        action, "-" if score is None else score, "-" if size is None else format_bytes(size),
        (record.get("source") or {}).get("name") or "?", ",".join(record.get("flags") or []) or "-")


def plan_paths(tools, paths, options, progress=None):
    """Plan every source under `paths`; print per-file lines, a table and the summary; return the StageSummary.

    A folder whose lock is held by another run, or whose lock cannot be
    created (a read-only folder), is skipped and counted as 'skipped'; the
    second kind is also listed in the summary's `lock_errors` (folder names),
    which makes run() exit with EXIT_ERROR.

    options.jobs above 1 plans that many items at once, also within one
    folder, through a thread pool; counts, rows and the folder file are
    handled on the calling thread from the values the workers return, and
    lines go through one line stream, as in convert --jobs. A threading.Lock
    per folder is held around each item's record step (reread its record,
    find the folder's records claiming the same output name, decide, save,
    quarantine a claiming record); the probe, hash, analysis and output-name
    check run outside it and touch no record. Items whose output names match
    can affect each other's records only through that step, so they go to
    one worker in list order; every other pair reads and writes disjoint
    records, which is why the records equal those of a sequential run.
    """
    from vidfunnel.encode import _LineStream
    stream, quiet = _progress_options(progress)
    jobs = max(1, int(getattr(options, "jobs", 1) or 1))
    if jobs > 1:
        stream = _LineStream(stream if stream is not None else sys.stdout)
        progress = types.SimpleNamespace(stream=stream, quiet=quiet)
    summary = StageSummary("plan", OUTCOMES)
    summary.lock_errors = []
    capabilities = dict(ffmpeg_capabilities(tools))
    capabilities["decodable"] = decodable_codecs(tools)
    say("scanning {} path(s) for sources".format(len(paths)), stream)
    sources = list(iter_sources(paths, tools))
    items = _expand(tools, sources, capabilities, progress)
    folders = []
    by_folder = {}
    for work, found in items:
        key = str(work.folder)
        if key not in by_folder:
            by_folder[key] = []
            folders.append(key)
        by_folder[key].append((work, found))
    siblings = {}
    for source in sources:
        siblings.setdefault(str(source.parent), []).append(source)
    total = len(items)
    if jobs > 1:
        rows = _plan_parallel(tools, options, progress, stream, summary, capabilities, folders, by_folder, siblings,
                              total, jobs)
    else:
        rows = _plan_sequential(tools, options, progress, stream, summary, capabilities, folders, by_folder,
                                siblings, total)
    if rows:
        say("  {:<34} {:>5} {:>10}  {}  {}".format("action", "score", "size", "source", "flags"), stream)
        for row in rows:
            say(row, stream)
    say(summary.format("'{}/' under each folder".format(RECORD_DIR)), stream)
    return summary


def _take_folder_lock(folder, entries, summary, stream):
    """The acquired FolderLock of `folder`, or None after saying why and counting its entries as skipped."""
    lock = FolderLock(folder, warn=lambda message: say(message, stream))
    try:
        lock.acquire()
    except LockHeld as error:
        say("skipping folder '{}': {}".format(folder, error), stream)
        summary.add("skipped", len(entries))
        return None
    except OSError as error:
        say("skipping folder '{}': cannot take its lock: {}".format(folder, error), stream)
        summary.add("skipped", len(entries))
        summary.lock_errors.append(folder)
        return None
    return lock


def _plan_entry(tools, work, found, options, folder_sources, capabilities, progress, stream, position, total,
                record_lock=None):
    """(outcome, record or None) for one work item, with its start line and its skip line.

    Every line of the item, the live progress lines of its passes included,
    starts with its tag '[plan <position>/<total>]'.
    """
    _stream, quiet = _progress_options(progress)
    stream = tagged(stream, item_tag("plan", position, total))
    progress = types.SimpleNamespace(stream=stream, quiet=quiet)
    say("probing '{}'".format(work.label), stream)
    try:
        outcome, record = _plan_one(tools, work, options, folder_sources, capabilities, progress, found,
                                    record_lock)
    except RecordError as error:
        say("failed: '{}': {}".format(work.label, error), stream)
        return "failed", None
    if outcome == "skipped":
        say("  skipped: record {} is '{}' and the source is unchanged (--replan to plan again)".format(
            record.get("id"), record.get("status")), stream)
    return outcome, record


def _plan_sequential(tools, options, progress, stream, summary, capabilities, folders, by_folder, siblings, total):
    """One folder after another, one item after another; returns the table rows."""
    position = 0
    rows = []
    for folder_number, folder in enumerate(folders):
        entries = by_folder[folder]
        if drain_before_next(sum(len(by_folder[later]) for later in folders[folder_number:]), folder):
            break
        lock = _take_folder_lock(folder, entries, summary, stream)
        if lock is None:
            position += len(entries)
            continue
        drained = False
        try:
            for entry_number, (work, found) in enumerate(entries):
                later = sum(len(by_folder[after]) for after in folders[folder_number + 1:])
                if drain_before_next(len(entries) - entry_number + later, folder):
                    drained = True
                    break
                position += 1
                outcome, record = _plan_entry(tools, work, found, options, siblings.get(folder, []), capabilities,
                                              progress, stream, position, total)
                drain_after_item()
                summary.add(outcome)
                if record is not None:
                    rows.append(_table_row(outcome, record))
            if not drained:
                _update_folder_file(folder, [work for work, _found in entries])
        finally:
            lock.release()
        if drained:
            break
    return rows


def _output_name_key(work):
    """(folder, output name casefolded): items sharing it may claim each other's output, and are planned together."""
    stem = work.stem if work.dvd_title is not None else output_stem(work.key)
    return str(work.folder), (stem + ".mp4").casefold()


def _plan_parallel(tools, options, progress, stream, summary, capabilities, folders, by_folder, siblings, total,
                   jobs):
    """The items of every locked folder through a pool of `jobs` threads; returns the rows in list order.

    One task per item, except that items with the same output name in one
    folder share a task and run one after another in list order. A
    folder's file is updated and its lock released on this thread as soon as
    its last group has returned, so a finished folder is open to convert
    while the others are still planned.

    Ctrl+C: vidfunnel.interrupt.interrupted is set by run_pool on this
    thread when KeyboardInterrupt arrives, and read by every ffmpeg or
    ffprobe start and exit in the workers (and the hash loop), which then
    raise Interrupted. _plan_one saves a record only at the end of an item
    and Interrupted is not among the errors it records as 'failed', so an
    interrupted item leaves its record as it was; groups not yet started are
    not started, and the folders still open keep their folder file unchanged.
    """
    folder_locks = {}
    groups = {}
    order = []
    position = 0
    try:
        for folder in folders:
            entries = by_folder[folder]
            lock = _take_folder_lock(folder, entries, summary, stream)
            if lock is None:
                position += len(entries)
                continue
            folder_locks[folder] = lock
            for work, found in entries:
                position += 1
                key = _output_name_key(work)
                if key not in groups:
                    groups[key] = []
                    order.append(key)
                groups[key].append((folder, work, found, position))
        record_locks = {folder: threading.Lock() for folder in folder_locks}
        outstanding = {}
        for key in order:
            folder = groups[key][0][0]
            outstanding[folder] = outstanding.get(folder, 0) + 1

        def plan_group(members):
            results = []
            for folder, work, found, number in members:
                results.append((number, _plan_entry(tools, work, found, options, siblings.get(folder, []),
                                                    capabilities, progress, stream, number, total,
                                                    record_locks[folder])))
            return results

        results = []

        def group_done(index, group_results):
            results.extend(group_results)
            folder = groups[order[index]][0][0]
            outstanding[folder] -= 1
            if outstanding[folder] == 0:
                _update_folder_file(folder, [work for work, _found in by_folder[folder]])
                folder_locks.pop(folder).release()

        run_pool(jobs, [lambda members=groups[key]: plan_group(members) for key in order], group_done,
                 sizes=[len(groups[key]) for key in order], folders=[groups[key][0][0] for key in order])
        rows = []
        for _number, (outcome, record) in sorted(results, key=lambda entry: entry[0]):
            summary.add(outcome)
            if record is not None:
                rows.append(_table_row(outcome, record))
        return rows
    finally:
        for lock in folder_locks.values():
            lock.release()


def _update_folder_file(folder, works):
    data = load_folder(folder)
    names = set(data.get("sources") or [])
    names.update(work.key.name for work in works)
    data.update(last_scan=now_iso(), tool_version=vidfunnel.VERSION, sources=sorted(names))
    save_folder(folder, data)


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


def add_arguments(parser):
    parser.add_argument("paths", nargs="*", metavar="PATH",
                        help="files or folders to plan (folders are walked; default: the current directory)")
    parser.add_argument("--samples", type=_positive_int, default=None, metavar="N",
                        help="sample windows for idet and cropdetect (default 12)")
    parser.add_argument("--full-analysis", action="store_true", help="analyse the whole file instead of samples")
    parser.add_argument("--assume-dar", choices=DARS, default=None, metavar="R",
                        help="resolve an ambiguous 4:3/16:9 frame as R (4:3 or 16:9)")
    parser.add_argument("--audio-offset", type=float, default=None, metavar="S",
                        help="audio start relative to video, in seconds, replacing the measured offset")
    parser.add_argument("--audio-strict-aac", action="store_true", help="re-encode MP3 to AAC instead of copying")
    parser.add_argument("--reencode-video", action="store_true", help="re-encode H.264 that could be remuxed")
    parser.add_argument("--max-level", choices=MAX_LEVELS, default="4.2",
                        help="4.1 deinterlaces HD at frame rate to stay at level 4.1 (default 4.2)")
    parser.add_argument("--plain-mp4", action="store_true", help="conventional +faststart MP4 instead of fragmented")
    parser.add_argument("--par2", type=_positive_int, default=None, metavar="N",
                        help="record that convert should write PAR2 with N %% redundancy")
    parser.add_argument("--replan", action="store_true", help="plan again sources whose records still match")
    parser.add_argument("--mark-outputs", action="store_true",
                        help="write every output (and its subtitle sidecars) as '<stem>.vidfunnel.mp4' until retire "
                        "renames it to '<stem>.mp4', so converted files are recognisable while the originals remain")
    parser.add_argument("--jobs", type=_positive_int, default=1, metavar="N",
                        help="files planned at once, also within one folder (default 1)")
    return parser


def run(args):
    """`vidfunnel plan`: EXIT_OK, EXIT_ERROR when any file failed or a folder could not be locked, EXIT_USAGE,
    EXIT_DEPS."""
    from vidfunnel.cli import EXIT_DEPS, EXIT_ERROR, EXIT_OK, EXIT_USAGE, paths_or_current_directory
    from vidfunnel.deps import DependencyError, require
    paths_or_current_directory(args, "plan")
    try:
        tools = require(args)
    except DependencyError as error:
        print("vidfunnel plan: {}".format(error), file=sys.stderr)
        return EXIT_DEPS
    missing = [path for path in args.paths if not os.path.exists(path)]
    if missing:
        for path in missing:
            print("vidfunnel plan: '{}' does not exist".format(path), file=sys.stderr)
        return EXIT_USAGE
    summary = plan_paths(tools, args.paths, options_from_args(args), progress=args)
    if summary.counts.get("failed") or getattr(summary, "lock_errors", None):
        return EXIT_ERROR
    return EXIT_OK

# End of File #
