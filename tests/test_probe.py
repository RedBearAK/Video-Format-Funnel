"""Tests for vidfunnel.probe: pure parsers, the fixture set through ffprobe, the captured JSON.

The fixture tests run probe_file on every clip in tests/fixtures (made by
tests/make_fixtures.sh) and skip with a message when ffmpeg or the fixtures
are missing. The captured-JSON tests need neither: they read
tests/probe_json, which analyze's unit tests also use (HANDOFF section 9).

Recapture after changing probe.py or the fixtures, from the repo root:

    python3 -m tests.test_probe --capture
"""

VERSION = "20261102"

import json
import os
import shutil
import sys
import tempfile
import unittest

from vidfunnel.ffmpeg import FfmpegError, ToolNotFound, find_tools
from vidfunnel.probe import (
    classify,
    count_frames,
    decodable_codecs,
    is_dvd_structure,
    keyframe_spacing,
    level_text,
    normalise_probe,
    normalise_stream,
    parse_clock,
    parse_decoders,
    parse_fraction,
    parse_ratio,
    probe_file,
    spacing_from_packets,
    vob_group,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(REPO_ROOT, "tests", "fixtures")
CAPTURE_DIR = os.path.join(REPO_ROOT, "tests", "probe_json")
MEDIA_SKIP_NAMES = ("README.md",)

# Stream field -> allowed types (None always allowed for the optional ones).
STREAM_FIELD_TYPES = {
    "index": (int,),
    "type": (str,),
    "codec": (str, type(None)),
    "codec_long": (str, type(None)),
    "codec_tag": (str, type(None)),
    "profile": (str, type(None)),
    "level": (int, type(None)),
    "level_text": (str, type(None)),
    "pix_fmt": (str, type(None)),
    "width": (int, type(None)),
    "height": (int, type(None)),
    "sar": (str, type(None)),
    "dar": (str, type(None)),
    "fps": (float, type(None)),
    "fps_fraction": (str, type(None)),
    "avg_fps": (float, type(None)),
    "avg_fps_fraction": (str, type(None)),
    "field_order": (str, type(None)),
    "has_b_frames": (int, type(None)),
    "color_range": (str, type(None)),
    "color_space": (str, type(None)),
    "color_primaries": (str, type(None)),
    "color_transfer": (str, type(None)),
    "duration": (float, type(None)),
    "duration_source": (str, type(None)),
    "tag_duration": (float, type(None)),
    "start_time": (float, type(None)),
    "bit_rate": (int, type(None)),
    "nb_frames": (int, type(None)),
    "channels": (int, type(None)),
    "channel_layout": (str, type(None)),
    "sample_rate": (int, type(None)),
    "sample_fmt": (str, type(None)),
    "language": (str, type(None)),
    "title": (str, type(None)),
    "tags": (dict,),
    "disposition": (dict,),
    "bits_per_raw_sample": (int, type(None)),
}


def fixture_files():
    """Every clip under tests/fixtures, as paths relative to it, sorted."""
    found = []
    if not os.path.isdir(FIXTURE_DIR):
        return found
    for folder, _, names in os.walk(FIXTURE_DIR):
        for name in names:
            if name in MEDIA_SKIP_NAMES:
                continue
            found.append(os.path.relpath(os.path.join(folder, name), FIXTURE_DIR))
    return sorted(found)


def capture_name(relative):
    return os.path.join(CAPTURE_DIR, relative + ".json")


def captured_probe(tools, relative):
    """probe_file output made portable: 'raw' removed, 'path' relative to the repo root."""
    probe = probe_file(tools, os.path.join(FIXTURE_DIR, relative))
    probe.pop("raw")
    probe["path"] = os.path.relpath(probe["path"], REPO_ROOT).replace(os.sep, "/")
    return probe


def capture_all(tools=None):
    """Write tests/probe_json/<fixture>.json for every fixture; returns the written paths."""
    tools = tools or find_tools()
    written = []
    for relative in fixture_files():
        probe = captured_probe(tools, relative)
        target = capture_name(relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(probe, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
        written.append(target)
    return written


def _tools_or_none():
    try:
        return find_tools()
    except ToolNotFound:
        return None


TOOLS = _tools_or_none()
FIXTURES = fixture_files()
SKIP_REASON = None
if TOOLS is None:
    SKIP_REASON = "ffmpeg/ffprobe not found on PATH; fixture probe tests skipped"
elif not FIXTURES:
    SKIP_REASON = "no fixtures in '{}' (run tests/make_fixtures.sh); fixture probe tests skipped".format(FIXTURE_DIR)


def raw_stream(**fields):
    base = {"index": 0, "codec_type": "video", "codec_name": "h264", "disposition": {"attached_pic": 0}}
    base.update(fields)
    return base


# --- pure ------------------------------------------------------------------------


class ParserTests(unittest.TestCase):
    def test_fraction(self):
        self.assertAlmostEqual(parse_fraction("30000/1001"), 29.97002997, places=6)
        self.assertEqual(parse_fraction("25/1"), 25.0)
        self.assertEqual(parse_fraction("25"), 25.0)
        for unknown in ("0/0", "N/A", None, "", "1/0", "abc/1"):
            self.assertIsNone(parse_fraction(unknown), unknown)

    def test_ratio(self):
        self.assertEqual(parse_ratio("32:27"), "32:27")
        for unknown in ("0:1", "N/A", None, "1:0", "4/3", ""):
            self.assertIsNone(parse_ratio(unknown), unknown)

    def test_clock(self):
        self.assertAlmostEqual(parse_clock("00:00:05.021000000"), 5.021)
        self.assertAlmostEqual(parse_clock("01:02:03.5"), 3723.5)
        self.assertIsNone(parse_clock("five seconds"))
        self.assertIsNone(parse_clock(None))

    def test_level_text(self):
        self.assertEqual(level_text("h264", 40), "4.0")
        self.assertEqual(level_text("h264", 41), "4.1")
        self.assertEqual(level_text("h264", 9), "1b")
        self.assertEqual(level_text("hevc", 93), "3.1")
        self.assertIsNone(level_text("mpeg2video", 8))
        self.assertIsNone(level_text("h264", None))

    def test_stream_unknowns_become_none(self):
        stream = normalise_stream(raw_stream(
            level=-99, sample_aspect_ratio="0:1", display_aspect_ratio="N/A", r_frame_rate="0/0",
            avg_frame_rate="0/0", color_space="unknown", nb_frames="N/A", codec_tag_string="[0][0][0][0]"))
        for key in ("level", "level_text", "sar", "dar", "fps", "fps_fraction", "avg_fps", "color_space",
                    "nb_frames", "codec_tag", "duration", "duration_source", "field_order", "start_time"):
            self.assertIsNone(stream[key], key)

    def test_stream_numbers_parsed(self):
        stream = normalise_stream(raw_stream(
            level=41, r_frame_rate="30000/1001", avg_frame_rate="24000/1001", start_time="0.279000",
            nb_frames="128", bit_rate="448000", bits_per_raw_sample="8", codec_tag_string="XVID",
            tags={"DURATION": "00:00:05.321000000", "language": "eng", "title": "Main"}))
        self.assertEqual(stream["level"], 41)
        self.assertEqual(stream["level_text"], "4.1")
        self.assertEqual(stream["fps_fraction"], "30000/1001")
        self.assertEqual(stream["avg_fps_fraction"], "24000/1001")
        self.assertAlmostEqual(stream["avg_fps"], 23.976, places=3)
        self.assertAlmostEqual(stream["start_time"], 0.279)
        self.assertEqual(stream["nb_frames"], 128)
        self.assertEqual(stream["bit_rate"], 448000)
        self.assertEqual(stream["bits_per_raw_sample"], 8)
        self.assertEqual(stream["codec_tag"], "XVID")
        self.assertAlmostEqual(stream["tag_duration"], 5.321)
        self.assertEqual(stream["language"], "eng")
        self.assertEqual(stream["title"], "Main")

    def test_duration_falls_back_to_format(self):
        own = normalise_stream(raw_stream(duration="4.004000"), format_duration=5.0)
        self.assertEqual((own["duration"], own["duration_source"]), (4.004, "stream"))
        borrowed = normalise_stream(raw_stream(), format_duration=5.0)
        self.assertEqual((borrowed["duration"], borrowed["duration_source"]), (5.0, "format"))

    def test_video_skips_cover_art(self):
        raw = {
            "format": {"format_name": "mp3", "duration": "3.0"},
            "streams": [
                {"index": 0, "codec_type": "audio", "codec_name": "mp3"},
                {"index": 1, "codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
            ],
        }
        probe = normalise_probe(raw, "/music/song.mp3")
        self.assertIsNone(probe["video"])
        self.assertEqual([stream["index"] for stream in probe["other"]], [1])
        self.assertEqual(classify(probe)[0], "no-video")

    def test_second_video_goes_to_other(self):
        raw = {"format": {}, "streams": [raw_stream(index=0), raw_stream(index=1)]}
        probe = normalise_probe(raw, "/x/two_angles.mkv")
        self.assertEqual(probe["video"]["index"], 0)
        self.assertEqual([stream["index"] for stream in probe["other"]], [1])


class ClassifyTests(unittest.TestCase):
    def probe_with(self, format_name, streams):
        return normalise_probe({"format": {"format_name": format_name}, "streams": streams}, "/x/clip")

    def test_swf_without_video_is_vector(self):
        probe = self.probe_with("swf", [{"index": 0, "codec_type": "audio", "codec_name": "mp3"}])
        self.assertEqual(classify(probe)[0], "swf-vector")

    def test_no_streams(self):
        kind, reason = classify(self.probe_with("avi", []))
        self.assertEqual(kind, "no-video")
        self.assertIn("none", reason)

    def test_unsupported_video_and_audio(self):
        probe = self.probe_with("avi", [raw_stream(index=0, codec_name="indeo5"),
                                        {"index": 1, "codec_type": "audio", "codec_name": "mysteryaudio"}])
        kind, reason = classify(probe, {"h264", "aac"})
        self.assertEqual(kind, "unsupported")
        self.assertIn("'indeo5'", reason)
        self.assertIn("'mysteryaudio'", reason)
        self.assertEqual(classify(probe, {"indeo5", "mysteryaudio"})[0], "video")
        self.assertEqual(classify(probe)[0], "video")

    def test_unidentified_codec_is_unsupported(self):
        probe = self.probe_with("avi", [{"index": 0, "codec_type": "video"}])
        kind, reason = classify(probe, {"h264"})
        self.assertEqual(kind, "unsupported")
        self.assertIn("not identified", reason)

    def test_parse_decoders_maps_codec_names(self):
        listing = (
            "Decoders:\n V..... = Video\n ------\n"
            " V....D flv                  FLV / Sorenson Spark (Flash Video) (codec flv1)\n"
            " VFS..D h264                 H.264 / AVC\n"
            " V..... h264_cuvid           Nvidia CUVID H264 decoder (codec h264)\n"
            " V..... vp9_cuvid            Nvidia CUVID VP9 decoder (codec vp9)\n"
            " A....D mp3float             MP3 (MPEG audio layer 3) (codec mp3)\n"
        )
        names = parse_decoders(listing)
        self.assertEqual(names, {"flv", "flv1", "h264", "mp3float", "mp3"})


class SpacingTests(unittest.TestCase):
    def test_constant_rate_gaps_and_tail(self):
        packets = []
        for frame in range(150):
            packets.append({"pts_time": "{:.6f}".format(frame * 0.04), "duration_time": "0.040000",
                            "flags": "K__" if frame % 50 == 0 else "___"})
        result = spacing_from_packets(packets)
        self.assertEqual(result["keyframes"], 3)
        self.assertAlmostEqual(result["max_gap_s"], 2.0)
        self.assertAlmostEqual(result["mean_gap_s"], 2.0)
        self.assertEqual(result["max_gap_frames"], 50)
        self.assertTrue(result["constant_frame_rate"])

    def test_single_keyframe_reports_tail(self):
        packets = [{"pts_time": str(frame * 0.04), "duration_time": "0.04", "flags": "K_" if frame == 0 else "__"}
                   for frame in range(100)]
        self.assertAlmostEqual(spacing_from_packets(packets)["max_gap_s"], 4.0)

    def test_dts_fallback_and_untimed(self):
        packets = [{"pts_time": "N/A", "dts_time": "0.0", "flags": "K_"},
                   {"pts_time": "N/A", "dts_time": "N/A", "flags": "__"},
                   {"pts_time": "N/A", "dts_time": "1.0", "flags": "K_"}]
        result = spacing_from_packets(packets)
        self.assertEqual(result["keyframes"], 2)
        self.assertEqual(result["untimed_packets"], 1)
        self.assertAlmostEqual(result["max_gap_s"], 1.0)

    def test_variable_rate(self):
        times = [step * 0.0667 for step in range(30)] + [2.0 + step * 0.0333 for step in range(60)]
        packets = [{"pts_time": str(seconds), "flags": "K_" if index % 30 == 0 else "__"}
                   for index, seconds in enumerate(times)]
        result = spacing_from_packets(packets)
        self.assertFalse(result["constant_frame_rate"])
        self.assertIsNone(result["frame_interval_s"])
        self.assertEqual(result["max_gap_frames"], 30)

    def test_one_skipped_frame_stays_constant(self):
        packets = [{"pts_time": str(frame * 0.04), "flags": "K_" if frame == 0 else "__"}
                   for frame in range(100) if frame != 1]
        result = spacing_from_packets(packets)
        self.assertTrue(result["constant_frame_rate"])
        self.assertAlmostEqual(result["frame_interval_s"], 0.04)

    def test_rate_unknown_when_pts_missing(self):
        packets = [{"pts_time": "N/A" if frame % 5 == 4 else str(frame * 0.04), "dts_time": str(frame * 0.04),
                    "flags": "K_" if frame == 0 else "__"} for frame in range(100)]
        result = spacing_from_packets(packets)
        self.assertIsNone(result["constant_frame_rate"])
        self.assertEqual(result["max_gap_frames"], 100)

    def test_no_keyframes(self):
        result = spacing_from_packets([{"pts_time": "0.0", "flags": "__"}])
        self.assertEqual(result["keyframes"], 0)
        self.assertIsNone(result["max_gap_s"])
        self.assertIsNone(result["mean_gap_s"])


class DvdStructureTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="vidfunnel-probe-")
        self.addCleanup(shutil.rmtree, self.folder)

    def touch(self, *parts):
        target = os.path.join(self.folder, *parts)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        open(target, "wb").close()
        return target

    def test_names(self):
        disc = os.path.join(self.folder, "VIDEO_TS")
        ifo = self.touch("VIDEO_TS", "VTS_01_0.IFO")
        menu = self.touch("VIDEO_TS", "VTS_01_0.VOB")
        title = self.touch("VIDEO_TS", "vts_02_3.vob")
        other = self.touch("VIDEO_TS", "movie.mpg")
        self.assertEqual(is_dvd_structure(disc), "video_ts")
        self.assertEqual(is_dvd_structure(ifo), "ifo")
        self.assertEqual(is_dvd_structure(menu), "vob_menu")
        self.assertEqual(is_dvd_structure(title), "vob")
        self.assertIsNone(is_dvd_structure(other))
        self.assertIsNone(is_dvd_structure(self.folder))

    def test_folder_with_video_ts_ifo(self):
        self.touch("disc_copy", "VIDEO_TS.IFO")
        self.assertEqual(is_dvd_structure(os.path.join(self.folder, "disc_copy")), "video_ts")

    def test_vob_group(self):
        for name in ("VTS_01_0.VOB", "VTS_01_2.VOB", "VTS_01_1.VOB", "VTS_01_3.VOB", "VTS_02_1.VOB"):
            self.touch(name)
        expected = [os.path.join(self.folder, name) for name in ("VTS_01_1.VOB", "VTS_01_2.VOB", "VTS_01_3.VOB")]
        self.assertEqual(vob_group(os.path.join(self.folder, "VTS_01_2.VOB")), expected)
        self.assertEqual(vob_group(os.path.join(self.folder, "VTS_02_1.VOB")),
                         [os.path.join(self.folder, "VTS_02_1.VOB")])
        with self.assertRaises(ValueError):
            vob_group(os.path.join(self.folder, "VTS_01_0.VOB"))


# --- captured JSON (no ffmpeg) ------------------------------------------------------------


def captured_files():
    found = []
    if os.path.isdir(CAPTURE_DIR):
        for folder, _, names in os.walk(CAPTURE_DIR):
            found.extend(os.path.join(folder, name) for name in names if name.endswith(".json"))
    return sorted(found)


@unittest.skipUnless(captured_files(), "no captured probe JSON in '{}'".format(CAPTURE_DIR))
class CapturedJsonTests(unittest.TestCase):
    def test_stream_fields_and_types(self):
        for capture in captured_files():
            with open(capture, encoding="utf-8") as handle:
                probe = json.load(handle)
            name = os.path.relpath(capture, CAPTURE_DIR)
            self.assertNotIn("raw", probe, name)
            self.assertFalse(os.path.isabs(probe["path"]), name)
            for key in ("path", "size", "format", "streams", "video", "audio", "subtitles", "other", "chapters"):
                self.assertIn(key, probe, name)
            grouped = ([probe["video"]] if probe["video"] else []) + probe["audio"] + probe["subtitles"] + probe["other"]
            self.assertEqual(sorted(stream["index"] for stream in grouped),
                             sorted(stream["index"] for stream in probe["streams"]), name)
            for stream in probe["streams"]:
                self.assertEqual(set(stream), set(STREAM_FIELD_TYPES), "{} stream {}".format(name, stream["index"]))
                for field, allowed in STREAM_FIELD_TYPES.items():
                    value = stream[field]
                    # JSON has no float/int distinction for whole numbers such as 25.0 written as 25.0.
                    self.assertTrue(isinstance(value, allowed) and not isinstance(value, bool),
                                    "{} stream {} field '{}' is {!r}".format(name, stream["index"], field, value))

    def test_files_are_formatted(self):
        for capture in captured_files():
            with open(capture, encoding="utf-8") as handle:
                text = handle.read()
            self.assertTrue(text.endswith("}\n"), capture)
            self.assertEqual(text, json.dumps(json.loads(text), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                             capture)


# --- fixtures through ffprobe ----------------------------------------------------------------


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class FixtureProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probes = {}
        for relative in FIXTURES:
            cls.probes[relative] = probe_file(TOOLS, os.path.join(FIXTURE_DIR, relative))

    def probe(self, relative):
        if relative not in self.probes:
            self.skipTest("fixture '{}' was not made".format(relative))
        return self.probes[relative]

    def test_every_fixture_probes(self):
        for relative, probe in self.probes.items():
            self.assertTrue(probe["streams"], relative)
            self.assertIn("raw", probe)
            self.assertTrue(os.path.isabs(probe["path"]))
            self.assertEqual(probe["size"], os.path.getsize(os.path.join(FIXTURE_DIR, relative)))

    def test_xvid(self):
        video = self.probe("xvid_avi_mp3.avi")["video"]
        self.assertEqual((video["codec"], video["codec_tag"]), ("mpeg4", "XVID"))
        self.assertEqual((video["sar"], video["dar"]), ("1:1", "4:3"))
        self.assertIsNone(video["field_order"])

    def test_h264_high(self):
        video = self.probe("h264_high_mkv_aac.mkv")["video"]
        self.assertEqual(video["profile"], "High")
        self.assertEqual(video["field_order"], "progressive")
        self.assertIsNone(video["nb_frames"])
        self.assertEqual(video["duration_source"], "format")
        self.assertAlmostEqual(video["tag_duration"], 5.0)

    def test_mpeg2_dar_16x9(self):
        video = self.probe("mpeg2_ps_ntsc_dar_16x9_ac3.mpg")["video"]
        self.assertEqual((video["dar"], video["sar"]), ("16:9", "32:27"))
        self.assertEqual(video["fps_fraction"], "30000/1001")
        self.assertAlmostEqual(video["fps"], 29.97, places=2)

    def test_mpeg2_interlaced(self):
        self.assertEqual(self.probe("mpeg2_ps_interlaced_tff_ac3.mpg")["video"]["field_order"], "tt")

    def test_h264_1080i(self):
        video = self.probe("h264_1080i_tff_ts_ac3.ts")["video"]
        self.assertEqual((video["width"], video["height"]), (1920, 1080))
        self.assertEqual((video["level"], video["level_text"]), (40, "4.0"))
        self.assertEqual(video["field_order"], "tt")

    def test_palette(self):
        video = self.probe("palette_msrle_avi_160x120.avi")["video"]
        self.assertEqual((video["codec"], video["pix_fmt"]), ("msrle", "pal8"))

    def test_audio_offset(self):
        # ffprobe 7 reports the first packet (0.279 s, before the 1024-sample AAC
        # priming); ffprobe 8 applies the codec delay and reports the audible
        # start (0.300 s). Both describe the same file; analyze measures from
        # packets and is tested in test_analyze.
        audio = self.probe("h264_mkv_aac_offset_300ms.mkv")["audio"][0]
        distance = min(abs(audio["start_time"] - 0.279), abs(audio["start_time"] - 0.300))
        self.assertLessEqual(distance, 0.002, audio["start_time"])

    def test_five_point_one(self):
        audio = self.probe("h264_mkv_ac3_5point1.mkv")["audio"][0]
        self.assertEqual((audio["codec"], audio["channels"]), ("ac3", 6))

    def test_srt_subtitles(self):
        subtitles = self.probe("h264_mkv_srt_subtitles.mkv")["subtitles"]
        self.assertEqual([(stream["codec"], stream["language"]) for stream in subtitles], [("subrip", "eng")])

    def test_chapters(self):
        chapters = self.probe("h264_mkv_chapters.mkv")["chapters"]
        self.assertEqual([chapter["title"] for chapter in chapters], ["Opening", "Ending"])
        self.assertAlmostEqual(chapters[0]["end"], 2.5)

    def test_dvd_nav_stream_is_other(self):
        probe = self.probe("mpeg2_ps_ntsc_dar_4x3_ac3.mpg")
        self.assertEqual(probe["video"]["codec"], "mpeg2video")
        self.assertEqual([stream["codec"] for stream in probe["other"]], ["dvd_nav_packet"])

    def test_classify_fixtures(self):
        decoders = decodable_codecs(TOOLS)
        self.assertEqual(classify(self.probe("flv1_swf_mp3.swf"), decoders)[0], "video")
        for relative, probe in self.probes.items():
            self.assertEqual(classify(probe, decoders)[0], "video", relative)

    def test_keyframe_spacing_long_gop(self):
        path = os.path.join(FIXTURE_DIR, "h264_long_gop_mkv_aac.mkv")
        if not os.path.exists(path):
            self.skipTest("fixture 'h264_long_gop_mkv_aac.mkv' was not made")
        result = keyframe_spacing(TOOLS, path, 0)
        self.assertEqual(result["keyframes"], 2)
        self.assertAlmostEqual(result["max_gap_s"], 8.342, delta=0.05)
        self.assertTrue(result["constant_frame_rate"])
        self.assertEqual(result["max_gap_frames"], 250)

    def test_keyframe_spacing_two_seconds(self):
        path = os.path.join(FIXTURE_DIR, "h264_gop_2s_mkv_aac.mkv")
        if not os.path.exists(path):
            self.skipTest("fixture 'h264_gop_2s_mkv_aac.mkv' was not made")
        result = keyframe_spacing(TOOLS, path, 0)
        self.assertEqual(result["keyframes"], 3)
        self.assertAlmostEqual(result["max_gap_s"], 2.0, delta=0.05)
        self.assertEqual(result["max_gap_frames"], 50)

    def test_keyframe_spacing_vfr(self):
        path = os.path.join(FIXTURE_DIR, "h264_mkv_vfr_aac.mkv")
        if not os.path.exists(path):
            self.skipTest("fixture 'h264_mkv_vfr_aac.mkv' was not made")
        result = keyframe_spacing(TOOLS, path, 0)
        self.assertFalse(result["constant_frame_rate"])
        self.assertEqual(result["keyframes"], 2)

    def test_keyframe_spacing_rate_judgement(self):
        cases = (("xvid_avi_mp3.avi", 0, True), ("mpeg2_ps_ntsc_dar_4x3_ac3.mpg", 1, None))
        for relative, index, expected in cases:
            path = os.path.join(FIXTURE_DIR, relative)
            if not os.path.exists(path):
                continue
            result = keyframe_spacing(TOOLS, path, index)
            self.assertEqual(result["constant_frame_rate"], expected, relative)
            self.assertLessEqual(result["max_gap_frames"], 18, relative)

    def test_count_frames(self):
        path = os.path.join(FIXTURE_DIR, "h264_mkv_aac_drift_long.mkv")
        if not os.path.exists(path):
            self.skipTest("fixture 'h264_mkv_aac_drift_long.mkv' was not made")
        self.assertEqual(count_frames(TOOLS, path, 0), 128)

    def test_unreadable_file_raises(self):
        folder = tempfile.mkdtemp(prefix="vidfunnel-probe-")
        self.addCleanup(shutil.rmtree, folder)
        bogus = os.path.join(folder, "not_a_video.avi")
        with open(bogus, "wb") as handle:
            handle.write(b"this is not a video file" * 10)
        with self.assertRaises(FfmpegError) as caught:
            probe_file(TOOLS, bogus)
        self.assertIn("'{}'".format(bogus), str(caught.exception))
        with self.assertRaises(FfmpegError):
            count_frames(TOOLS, bogus, 0)

    def test_loose_vob(self):
        path = os.path.join(FIXTURE_DIR, "vob_fragments_no_ifo", "VTS_01_1.VOB")
        if not os.path.exists(path):
            self.skipTest("fixture 'vob_fragments_no_ifo/VTS_01_1.VOB' was not made")
        self.assertEqual(is_dvd_structure(path), "vob")
        folder = os.path.dirname(path)
        self.assertEqual(vob_group(path), [os.path.join(folder, "VTS_01_1.VOB"), os.path.join(folder, "VTS_01_2.VOB")])


def main(argv):
    if "--capture" in argv:
        written = capture_all()
        for target in written:
            print("wrote '{}'".format(os.path.relpath(target, REPO_ROOT)))
        print("{} files".format(len(written)))
        return 0
    unittest.main(argv=[argv[0]] + [arg for arg in argv[1:]])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

# End of File #
