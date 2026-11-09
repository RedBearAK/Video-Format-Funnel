"""ffprobe wrapper: the normalised stream model that analyze and plan read.

HANDOFF.md sections 3, 4.2, 4.6, 4.8 and 7. probe_file() turns ffprobe's
JSON into a Probe dict with one flat Stream dict per stream, numbers parsed
and "unknown" spelled as None, and keeps the raw JSON under 'raw'. The other
functions measure what a plain probe cannot tell: keyframe spacing from the
packet list (a demux pass, no decoding), an exact packet count, whether the
file is something ffmpeg can convert at all, and whether a path is part of a
DVD structure.

Values are reported as ffprobe gives them; nothing is estimated here (no
frame count from duration times frame rate, no aspect guessed from the frame
size). The rules that interpret the values live in analyze.
"""

VERSION = "20261109"

import os
import re

from vidfunnel.ffmpeg import FfmpegError, ffprobe_json, ffprobe_packets, run
from vidfunnel.progress import say

# Files at least this large get a start line before a packet scan or count,
# which reads the whole file and can take more than a second.
ANNOUNCE_BYTES = 32 * 1024 * 1024

# A packet interval is regular when it is within this many seconds plus
# FRAME_INTERVAL_RELATIVE of the median interval. Matroska's millisecond time
# base turns 29.97 fps into alternating 33 and 34 ms steps.
FRAME_INTERVAL_TOLERANCE_S = 0.0015
FRAME_INTERVAL_RELATIVE = 0.01
# Constant frame rate: at least this share of the intervals is regular (one
# skipped frame in an AVI must not make it variable; a 15-then-30 fps join must).
CONSTANT_RATE_SHARE = 0.95
# The rate is judged only when at least this share of packets carries a pts;
# MPEG-PS leaves pts off some packets and dts would mix decode and display order.
TIMED_PACKET_SHARE = 0.9

STREAM_TYPES = ("video", "audio", "subtitle", "data", "attachment")

# Decoders that need particular hardware; their presence does not make a codec
# decodable on an ordinary machine.
_HARDWARE_DECODER_SUFFIXES = ("_cuvid", "_qsv", "_v4l2m2m", "_mediacodec", "_mmal", "_rkmpp", "_vaapi",
                              "_videotoolbox", "_amf", "_ohcodec")

_DVD_IFO = re.compile(r"^VTS_(\d\d)_0\.IFO$", re.IGNORECASE)
_DVD_TITLE_VOB = re.compile(r"^VTS_(\d\d)_([1-9])\.VOB$", re.IGNORECASE)
_DVD_MENU_VOB = re.compile(r"^(?:VTS_\d\d_0|VIDEO_TS)\.VOB$", re.IGNORECASE)
_DECODER_CODEC = re.compile(r"\(codec ([A-Za-z0-9_]+)\)\s*$")

_decodable_cache = {}


# --- value parsing ---------------------------------------------------------------


def _text(value):
    """A string value, or None for missing, empty, 'N/A' and 'unknown'."""
    if value is None:
        return None
    value = str(value).strip()
    if value in ("", "N/A", "unknown"):
        return None
    return value


def _int(value):
    text = _text(value)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        try:
            return int(float(text))
        except ValueError:
            return None


def _float(value):
    text = _text(value)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_fraction(text):
    """'30000/1001' -> 29.97002997..., '25' -> 25.0; None for '0/0', 'N/A' or a zero denominator."""
    text = _text(text)
    if text is None:
        return None
    numerator, _, denominator = text.partition("/")
    try:
        top = float(numerator)
        bottom = float(denominator) if denominator else 1.0
    except ValueError:
        return None
    if bottom == 0 or top == 0:
        return None
    return top / bottom


def _fraction_text(text):
    """The fraction string as ffprobe gave it, or None when it means 'unknown' ('0/0')."""
    return _text(text) if parse_fraction(text) is not None else None


def parse_ratio(text):
    """'32:27' -> '32:27'; None for '0:1', 'N/A', missing or malformed."""
    text = _text(text)
    if text is None:
        return None
    match = re.match(r"^(\d+):(\d+)$", text)
    if match is None:
        return None
    if int(match.group(1)) == 0 or int(match.group(2)) == 0:
        return None
    return text


def parse_clock(text):
    """Matroska's 'HH:MM:SS.nnnnnnnnn' DURATION tag -> float seconds, or None."""
    text = _text(text)
    if text is None:
        return None
    match = re.match(r"^(\d+):(\d+):(\d+(?:\.\d+)?)$", text)
    if match is None:
        return None
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def level_text(codec, level):
    """The level as people write it: h264 41 -> '4.1', h264 9 -> '1b', hevc 93 -> '3.1'; else None.

    Other codecs (MPEG-2 gives 8 for Main level, 4 for High) have no dotted
    form; their integer stays in 'level'.
    """
    if level is None or level <= 0:
        return None
    if codec == "h264":
        if level == 9:
            return "1b"
        return "{}.{}".format(level // 10, level % 10)
    if codec == "hevc" and level % 3 == 0:
        tenths = level // 3
        return "{}.{}".format(tenths // 10, tenths % 10)
    return None


def _tag(tags, name):
    """A tag value looked up case-insensitively (MKV writes 'DURATION', MP4 'language')."""
    for key, value in tags.items():
        if key.lower() == name.lower():
            return value
    return None


# --- the Probe -------------------------------------------------------------------


def normalise_stream(raw_stream, format_duration=None):
    """One Stream dict from one ffprobe stream entry. Pure.

    'duration' is the stream's own duration, else the format's
    ('duration_source' says which: 'stream', 'format' or None). Matroska and
    WebM streams carry no duration field; their DURATION tag is reported
    separately as 'tag_duration', because it is the end time of the stream's
    last packet (rounded to the millisecond), not a length: with the audio
    starting at 0.279 s it reads 5.321 for 5.04 s of audio.
    """
    tags = dict(raw_stream.get("tags") or {})
    codec_type = raw_stream.get("codec_type")
    codec = _text(raw_stream.get("codec_name"))
    level = _int(raw_stream.get("level"))
    if level is not None and level < 0:
        level = None
    duration = _float(raw_stream.get("duration"))
    duration_source = "stream" if duration is not None else None
    if duration is None and format_duration is not None:
        duration = format_duration
        duration_source = "format"
    codec_tag = _text(raw_stream.get("codec_tag_string"))
    if codec_tag is not None and codec_tag.startswith("[0]"):
        codec_tag = None
    disposition = {key: int(value) for key, value in (raw_stream.get("disposition") or {}).items()}
    return {
        "index": raw_stream.get("index"),
        "type": codec_type if codec_type in STREAM_TYPES else (codec_type or "unknown"),
        "codec": codec,
        "codec_long": _text(raw_stream.get("codec_long_name")),
        "codec_tag": codec_tag,
        "profile": _text(raw_stream.get("profile")),
        "level": level,
        "level_text": level_text(codec, level),
        "pix_fmt": _text(raw_stream.get("pix_fmt")),
        "width": _int(raw_stream.get("width")),
        "height": _int(raw_stream.get("height")),
        "sar": parse_ratio(raw_stream.get("sample_aspect_ratio")),
        "dar": parse_ratio(raw_stream.get("display_aspect_ratio")),
        "fps": parse_fraction(raw_stream.get("r_frame_rate")),
        "fps_fraction": _fraction_text(raw_stream.get("r_frame_rate")),
        "avg_fps": parse_fraction(raw_stream.get("avg_frame_rate")),
        "avg_fps_fraction": _fraction_text(raw_stream.get("avg_frame_rate")),
        "field_order": _text(raw_stream.get("field_order")),
        "has_b_frames": _int(raw_stream.get("has_b_frames")),
        "color_range": _text(raw_stream.get("color_range")),
        "color_space": _text(raw_stream.get("color_space")),
        "color_primaries": _text(raw_stream.get("color_primaries")),
        "color_transfer": _text(raw_stream.get("color_transfer")),
        "duration": duration,
        "duration_source": duration_source,
        "tag_duration": parse_clock(_tag(tags, "DURATION")),
        "start_time": _float(raw_stream.get("start_time")),
        "bit_rate": _int(raw_stream.get("bit_rate")),
        "nb_frames": _int(raw_stream.get("nb_frames")),
        "channels": _int(raw_stream.get("channels")),
        "channel_layout": _text(raw_stream.get("channel_layout")),
        "sample_rate": _int(raw_stream.get("sample_rate")),
        "sample_fmt": _text(raw_stream.get("sample_fmt")),
        "language": _text(_tag(tags, "language")),
        "title": _text(_tag(tags, "title")),
        "tags": tags,
        "disposition": disposition,
        "bits_per_raw_sample": _int(raw_stream.get("bits_per_raw_sample")),
    }


def normalise_probe(raw, path, size=None):
    """The Probe dict from parsed ffprobe JSON. Pure: no file access, no ffmpeg.

    'video' is the first video stream whose disposition is not attached_pic
    (cover art); 'audio', 'subtitles' and 'other' (data, attachments, cover
    art) list the remaining streams in file order.
    """
    raw_format = raw.get("format") or {}
    format_duration = _float(raw_format.get("duration"))
    streams = [normalise_stream(entry, format_duration) for entry in raw.get("streams") or []]
    video = None
    audio = []
    subtitles = []
    other = []
    for stream in streams:
        if stream["type"] == "video" and not stream["disposition"].get("attached_pic"):
            if video is None:
                video = stream
            else:
                other.append(stream)
        elif stream["type"] == "audio":
            audio.append(stream)
        elif stream["type"] == "subtitle":
            subtitles.append(stream)
        else:
            other.append(stream)
    chapters = []
    for chapter in raw.get("chapters") or []:
        chapters.append({
            "start": _float(chapter.get("start_time")),
            "end": _float(chapter.get("end_time")),
            "title": _text(_tag(chapter.get("tags") or {}, "title")),
        })
    return {
        "path": path,
        "size": size,
        "format": {
            "name": _text(raw_format.get("format_name")),
            "long_name": _text(raw_format.get("format_long_name")),
            "duration": format_duration,
            "bit_rate": _int(raw_format.get("bit_rate")),
            "start_time": _float(raw_format.get("start_time")),
            "tags": dict(raw_format.get("tags") or {}),
            "nb_streams": _int(raw_format.get("nb_streams")),
        },
        "streams": streams,
        "video": video,
        "audio": audio,
        "subtitles": subtitles,
        "other": other,
        "chapters": chapters,
        "raw": raw,
    }


def probe_file(tools, path, extra_args=()):
    """Probe `path` with ffprobe and return the normalised Probe dict.

    extra_args go before the input (for example ('-f', 'dvdvideo', '-title',
    '1') for a DVD title). 'path' is absolute, 'size' the file size in bytes
    (None for a directory). Raises FfmpegError naming the file when ffprobe
    cannot read it.
    """
    absolute = os.path.abspath(os.fspath(path))
    raw = ffprobe_json(tools, absolute, extra_args)
    size = None if os.path.isdir(absolute) else os.path.getsize(absolute)
    return normalise_probe(raw, absolute, size)


# --- packet-level measurements -----------------------------------------------------


def _announce(path, what, progress=None):
    """One start line before a whole-file pass over a large file, on the stream of `progress` when it has one.

    Through that stream the line goes through --jobs' line stream and carries
    the item's tag, as the item's other lines do (stdout without one).
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size >= ANNOUNCE_BYTES:
        say("{} of '{}' ({:.0f} MiB)".format(what, os.path.basename(path), size / (1024.0 * 1024.0)),
            getattr(progress, "stream", None))


def _packet_time(packet):
    seconds = _float(packet.get("pts_time"))
    if seconds is None:
        seconds = _float(packet.get("dts_time"))
    return seconds


def spacing_from_packets(packets):
    """Keyframe spacing from packet dicts with pts_time, dts_time, duration_time and flags. Pure.

    `packets` are in file (decode) order, as ffprobe lists them. Keyframes
    are the packets with 'K' in flags, timed by pts_time, or dts_time when
    pts_time is missing. The gaps are the intervals between successive
    keyframes plus the interval from the last keyframe to the end of the last
    packet, so a file with one keyframe still reports how far a seek may have
    to decode. 'max_gap_frames' is the largest number of packets from one
    keyframe up to the next (or to the end), in file order: the GOP length,
    exact for any frame rate, as one video packet is one frame.

    'constant_frame_rate' is True when at least CONSTANT_RATE_SHARE of the
    intervals between successive pts (sorted) are within
    FRAME_INTERVAL_TOLERANCE_S plus FRAME_INTERVAL_RELATIVE of the median
    interval, False when fewer are, and None when fewer than
    TIMED_PACKET_SHARE of the packets carry a pts (MPEG-PS) or there are
    fewer than three. 'frame_interval_s' is the mean of the regular
    intervals when the rate is constant.

    Returns {'keyframes', 'packets', 'untimed_packets', 'max_gap_s',
    'mean_gap_s', 'max_gap_frames', 'constant_frame_rate',
    'frame_interval_s'}; the gap values are None when there is no keyframe.
    """
    keyframe_times = []
    keyframe_positions = []
    presentation_times = []
    end = None
    untimed = 0
    for position, packet in enumerate(packets):
        presentation = _float(packet.get("pts_time"))
        if presentation is not None:
            presentation_times.append(presentation)
        seconds = presentation if presentation is not None else _float(packet.get("dts_time"))
        if seconds is None:
            untimed += 1
        else:
            packet_end = seconds + (_float(packet.get("duration_time")) or 0.0)
            end = packet_end if end is None else max(end, packet_end)
        if "K" in (packet.get("flags") or ""):
            keyframe_positions.append(position)
            if seconds is not None:
                keyframe_times.append(seconds)
    keyframe_times.sort()

    constant = None
    interval = None
    if len(presentation_times) >= 3 and len(presentation_times) >= TIMED_PACKET_SHARE * len(packets):
        presentation_times.sort()
        steps = [later - earlier for earlier, later in zip(presentation_times, presentation_times[1:])]
        median = sorted(steps)[len(steps) // 2]
        tolerance = FRAME_INTERVAL_TOLERANCE_S + FRAME_INTERVAL_RELATIVE * median
        regular = [step for step in steps if abs(step - median) <= tolerance]
        constant = median > 0 and len(regular) >= CONSTANT_RATE_SHARE * len(steps)
        if constant:
            interval = sum(regular) / len(regular)

    gaps = [later - earlier for earlier, later in zip(keyframe_times, keyframe_times[1:])]
    if keyframe_times and end is not None and end > keyframe_times[-1]:
        gaps.append(end - keyframe_times[-1])
    group_lengths = [later - earlier for earlier, later in
                     zip(keyframe_positions, keyframe_positions[1:] + [len(packets)])]
    return {
        "keyframes": len(keyframe_positions),
        "packets": len(packets),
        "untimed_packets": untimed,
        "max_gap_s": max(gaps) if gaps else None,
        "mean_gap_s": sum(gaps) / len(gaps) if gaps else None,
        "max_gap_frames": max(group_lengths) if group_lengths else None,
        "constant_frame_rate": constant,
        "frame_interval_s": interval,
    }


def keyframe_spacing(tools, path, stream_index, progress=None):
    """Keyframe spacing of stream `stream_index` from its packet list (HANDOFF 3; demux only).

    See spacing_from_packets for the result. `progress`, a
    vidfunnel.progress.Progress or None, gets an update before and after the
    scan (ffprobe reports no progress of its own while it reads packets).
    """
    absolute = os.path.abspath(os.fspath(path))
    _announce(absolute, "scanning keyframes of stream {}".format(stream_index), progress)
    if progress is not None:
        progress.update(extra="keyframe scan, stream {}".format(stream_index))
    packets = ffprobe_packets(tools, absolute, stream_index, "pts_time,dts_time,duration_time,flags")
    result = spacing_from_packets(packets)
    if progress is not None:
        progress.update(extra="{} keyframes in {} packets".format(result["keyframes"], result["packets"]))
    return result


def count_frames(tools, path, stream_index, progress=None):
    """Exact packet count of stream `stream_index` (`ffprobe -count_packets`, nb_read_packets).

    For video one packet is one frame (field-coded streams carry a frame per
    packet too). Raises FfmpegError when ffprobe fails or reports no count.
    `progress` is None, a vidfunnel.progress.Progress, or an item's progress
    namespace (its 'stream' only, as verify passes): the start line for a
    large file goes to its stream, and update() is called when it has one.
    """
    absolute = os.path.abspath(os.fspath(path))
    _announce(absolute, "counting packets of stream {}".format(stream_index), progress)
    update = getattr(progress, "update", None)
    if update is not None:
        update(extra="counting packets, stream {}".format(stream_index))
    argv = [
        tools.ffprobe,
        "-v",
        "error",
        "-count_packets",
        "-select_streams",
        str(stream_index),
        "-show_entries",
        "stream=nb_read_packets",
        "-print_format",
        "default=noprint_wrappers=1:nokey=1",
        absolute,
    ]
    completed = run(argv, check=False)
    text = completed.stdout.strip()
    count = _int(text.splitlines()[0]) if text else None
    if completed.returncode != 0 or count is None:
        raise FfmpegError(
            "ffprobe could not count the packets of stream {} in '{}' (status {}, output '{}'): {}".format(
                stream_index, absolute, completed.returncode, text, completed.stderr.strip()
            ),
            argv,
            completed.returncode,
            completed.stderr,
        )
    if update is not None:
        update(extra="{} packets".format(count))
    return count


# --- classification ------------------------------------------------------------------


def parse_decoders(listing):
    """Codec names that some software decoder handles, from `ffmpeg -decoders` output. Pure.

    A decoder whose name differs from its codec says so at the end of its
    description: 'flv ... (Flash Video) (codec flv1)'. Both names are kept.
    Hardware-only decoders (cuvid, qsv, v4l2m2m ...) are left out.
    """
    names = set()
    started = False
    for line in listing.splitlines():
        if not started:
            stripped = line.strip()
            started = bool(stripped) and set(stripped) == {"-"}
            continue
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        decoder = parts[1]
        if decoder.endswith(_HARDWARE_DECODER_SUFFIXES):
            continue
        names.add(decoder)
        match = _DECODER_CODEC.search(parts[2] if len(parts) > 2 else "")
        if match:
            names.add(match.group(1))
    return names


def decodable_codecs(tools):
    """Set of codec names the ffmpeg in `tools` can decode. Cached per ffmpeg path.

    vidfunnel.ffmpeg.capabilities()['decoders'] holds decoder names, which
    are not always codec names (codec flv1 is decoded by 'flv'); this set
    holds both and is what classify() checks ffprobe's codec_name against.
    """
    if tools.ffmpeg in _decodable_cache:
        return _decodable_cache[tools.ffmpeg]
    output = run([tools.ffmpeg, "-hide_banner", "-decoders"]).stdout
    names = parse_decoders(output)
    if not names:
        raise FfmpegError(
            "'{}' -decoders printed no entries; its output format was not understood".format(tools.ffmpeg),
            [tools.ffmpeg, "-hide_banner", "-decoders"],
            0,
            "",
        )
    _decodable_cache[tools.ffmpeg] = names
    return names


def classify(probe, decoders=None):
    """What kind of input this is: (kind, reason). Pure given the decoder set.

    kind is 'video' (convertible), 'swf-vector' (an SWF with no video stream:
    vector or ActionScript, HANDOFF 4.8), 'no-video' (nothing but audio,
    subtitles, data or cover art) or 'unsupported' (the video stream or an
    audio stream has a codec ffmpeg cannot decode, HANDOFF 4.1). `decoders`
    is the set from decodable_codecs(tools); None skips the decoder check.
    """
    video = probe.get("video")
    format_names = (probe.get("format", {}).get("name") or "").split(",")
    if video is None:
        if "swf" in format_names:
            return ("swf-vector", "SWF with no video stream: vector or ActionScript animation, nothing to decode")
        kinds = sorted(set(stream["type"] for stream in probe.get("streams", [])))
        return ("no-video", "no video stream (streams: {})".format(", ".join(kinds) if kinds else "none"))
    if decoders is not None:
        missing = []
        for stream in [video] + list(probe.get("audio", [])):
            codec = stream.get("codec")
            if codec is None or codec not in decoders:
                missing.append("stream {} ({}): {}".format(
                    stream.get("index"), stream.get("type"),
                    "codec not identified by ffprobe" if codec is None else "no decoder for '{}'".format(codec)))
        if missing:
            return ("unsupported", "; ".join(missing))
    return ("video", "video stream {} ({})".format(video.get("index"), video.get("codec")))


# --- DVD structures (HANDOFF 4.6) -----------------------------------------------------


def is_dvd_structure(path):
    """'video_ts', 'ifo', 'vob', 'vob_menu' or None for a path. Looks only at names, never inside files.

    'video_ts': a directory named VIDEO_TS, or one holding VIDEO_TS.IFO.
    'ifo': a VTS_xx_0.IFO file. 'vob': a title-set VOB, VTS_xx_N.VOB with N
    from 1 to 9. 'vob_menu': VTS_xx_0.VOB or VIDEO_TS.VOB, the menus, which
    are skipped (HANDOFF 4.6). Case-insensitive, as discs written on other
    systems often are lower case.
    """
    path = os.fspath(path)
    name = os.path.basename(os.path.normpath(path))
    if os.path.isdir(path):
        if name.upper() == "VIDEO_TS":
            return "video_ts"
        try:
            entries = os.listdir(path)
        except OSError:
            return None
        if any(entry.upper() == "VIDEO_TS.IFO" for entry in entries):
            return "video_ts"
        return None
    if _DVD_IFO.match(name):
        return "ifo"
    if _DVD_TITLE_VOB.match(name):
        return "vob"
    if _DVD_MENU_VOB.match(name):
        return "vob_menu"
    return None


def vob_group(path):
    """The title-set VOBs that `path` belongs to, sorted by part number, as absolute paths.

    For '.../VTS_01_2.VOB' this is every VTS_01_N.VOB (N 1 to 9) in the same
    folder, the menu VOB VTS_01_0.VOB excluded; plan concatenates them in this
    order (HANDOFF 4.6). A gap in the numbering is kept as found; the caller
    can see it from the names. Raises ValueError when `path` is not a
    title-set VOB name.
    """
    absolute = os.path.abspath(os.fspath(path))
    folder, name = os.path.split(absolute)
    match = _DVD_TITLE_VOB.match(name)
    if match is None:
        raise ValueError("'{}' is not a DVD title-set VOB (VTS_xx_N.VOB, N 1 to 9)".format(absolute))
    title_set = match.group(1)
    members = []
    for entry in os.listdir(folder):
        entry_match = _DVD_TITLE_VOB.match(entry)
        if entry_match and entry_match.group(1) == title_set and os.path.isfile(os.path.join(folder, entry)):
            members.append((int(entry_match.group(2)), entry))
    members.sort()
    return [os.path.join(folder, entry) for _, entry in members]

# End of File #
