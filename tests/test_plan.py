"""Tests for vidfunnel.plan: argv building from captured probe and analysis JSON, names, sources, and plan runs.

The pure tests read tests/probe_json and tests/analysis_json and need no
ffmpeg. The integration tests copy fixtures from tests/fixtures into a
temporary folder (plan writes '.vidfunnel/' beside its sources, so the
fixture folder itself is never planned) and skip with a message when
ffmpeg or the fixtures are missing.
"""

VERSION = "20261110"

import contextlib
import copy
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

from vidfunnel.analyze import decide_audio, decide_cadence, decide_subtitles, decide_sync
from vidfunnel.ffmpeg import Tools, ToolNotFound, find_tools
from vidfunnel.ffmpeg import run as run_tool
from vidfunnel.plan import (
    PROFILE,
    PlanError,
    build_argv,
    plan_size_entries,
    build_sidecar_argvs,
    crf_for_height,
    decide_action,
    identity_tag,
    input_arguments,
    iter_sources,
    make_options,
    output_names,
    plan_paths,
    video_filters,
)
from vidfunnel.record import add_flag, iter_records, load_record, record_path, save_record, set_status

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(REPO_ROOT, "tests", "fixtures")
PROBE_DIR = os.path.join(REPO_ROOT, "tests", "probe_json")
ANALYSIS_DIR = os.path.join(REPO_ROOT, "tests", "analysis_json")

FAKE_TOOLS = Tools("/opt/ffmpeg/bin/ffmpeg", "/opt/ffmpeg/bin/ffprobe")
# The time a source shows on the planning host, and what other mounts can show for it.
PLANNED_MTIME_NS = 1700000001987654321
SECOND_NS = 1000 * 1000 * 1000
STEPS = (("hundred_nanoseconds", 100), ("microsecond", 1000), ("millisecond", 1000 * 1000),
         ("ten_milliseconds", 10 * 1000 * 1000), ("second", SECOND_NS), ("two_seconds", 2 * SECOND_NS))


def truncated(value, step):
    return value - value % step


# (word, recorded time, seen time): one is the other truncated to a coarser step, in both directions.
COARSER_VIEWS = ([("seen_" + word, PLANNED_MTIME_NS, truncated(PLANNED_MTIME_NS, step)) for word, step in STEPS]
                 + [("recorded_" + word, truncated(PLANNED_MTIME_NS, step), PLANNED_MTIME_NS) for word, step in STEPS])
# (word, recorded time, seen time): differences that no truncation explains.
OTHER_TIMES = (
    ("second_later", PLANNED_MTIME_NS, PLANNED_MTIME_NS + SECOND_NS),
    ("second_and_half_later", PLANNED_MTIME_NS, PLANNED_MTIME_NS + 3 * SECOND_NS // 2),
    ("days_later", PLANNED_MTIME_NS, PLANNED_MTIME_NS + 3 * 86400 * SECOND_NS),
)
LARGE_FIXTURE = "qtrle_mov_rgb24_320x240.mov"     # 3.0 MiB: a middle that the first and last MiB do not cover


def flip_middle(path):
    """Flip one byte at 1.5 MiB: the size and the first and last MiB stay as they were."""
    with open(str(path), "r+b") as handle:
        handle.seek(1536 * 1024)
        value = handle.read(1)
        handle.seek(1536 * 1024)
        handle.write(bytes([value[0] ^ 0xFF]))
RECORD_ID = "0f2b6c1e-4d55-4a8e-9c1d-2f3e4a5b6c7d"
SOURCE_SHA = "0123456789abcdef" + "f" * 48
OUTPUT_PATH = "/library/clips/output.mp4"


def load_pair(name):
    with open(os.path.join(PROBE_DIR, name + ".json"), encoding="utf-8") as handle:
        probe = json.load(handle)
    with open(os.path.join(ANALYSIS_DIR, name + ".json"), encoding="utf-8") as handle:
        analysis = json.load(handle)
    return probe, analysis


def argv_for(name, options=None, analysis=None, action=None):
    probe, captured = load_pair(name)
    analysis = analysis if analysis is not None else captured
    options = options or make_options()
    if action is None:
        action, _reason = decide_action(probe, analysis, options, {})
    return build_argv(FAKE_TOOLS, ["/library/clips/" + name], OUTPUT_PATH, probe, analysis, action, options,
                      RECORD_ID, SOURCE_SHA)


def value_after(argv, option):
    """The argument following the first occurrence of `option`; fails the test when absent."""
    position = argv.index(option)
    return argv[position + 1]


def pairs(argv):
    return list(zip(argv, argv[1:]))


def quiet():
    return types.SimpleNamespace(stream=io.StringIO(), quiet=True)


# --- argv from captured JSON -----------------------------------------------------------


class InterlacedMpegTwoTest(unittest.TestCase):

    def setUp(self):
        self.argv = argv_for("mpeg2_ps_interlaced_tff_ac3.mpg")

    def test_reencode_with_field_rate_deinterlace(self):
        argv = self.argv
        self.assertEqual(argv[0], FAKE_TOOLS.ffmpeg)
        self.assertEqual(value_after(argv, "-c:v"), "libx264")
        self.assertIn("bwdif=mode=1:parity=0", value_after(argv, "-vf").split(","))
        self.assertEqual(value_after(argv, "-level"), "4.1")
        self.assertEqual(value_after(argv, "-crf"), "18")
        self.assertEqual(value_after(argv, "-preset"), "slower")
        self.assertEqual(value_after(argv, "-profile:v"), "high")
        self.assertEqual(value_after(argv, "-pix_fmt"), "yuv420p")
        self.assertEqual(value_after(argv, "-x264-params"), "open-gop=0")
        self.assertEqual(value_after(argv, "-force_key_frames"), "expr:gte(t,n_forced*2)")
        self.assertNotIn("-r", argv)

    def test_gop_follows_the_doubled_output_rate(self):
        # 29.97i deinterlaced at field rate is 59.94p: -g 2*fps and -keyint_min fps (HANDOFF 2.1).
        self.assertEqual(value_after(self.argv, "-g"), "120")
        self.assertEqual(value_after(self.argv, "-keyint_min"), "60")

    def test_audio_colour_aspect_container(self):
        argv = self.argv
        self.assertEqual(value_after(argv, "-c:a:0"), "aac")
        self.assertEqual(value_after(argv, "-b:a:0"), "192k")
        self.assertEqual(value_after(argv, "-filter:a:0"), "aresample=async=1")
        self.assertEqual(value_after(argv, "-color_range"), "tv")
        self.assertEqual(value_after(argv, "-colorspace"), "smpte170m")
        self.assertEqual(value_after(argv, "-color_primaries"), "smpte170m")
        self.assertEqual(value_after(argv, "-color_trc"), "smpte170m")
        self.assertEqual(value_after(argv, "-aspect"), "4:3")
        self.assertEqual(value_after(argv, "-vf"), "bwdif=mode=1:parity=0,setparams=range=tv:"
                         "color_primaries=smpte170m:color_trc=smpte170m:colorspace=smpte170m")
        self.assertEqual(value_after(argv, "-movflags"), "+frag_keyframe+empty_moov+default_base_moof+use_metadata_tags")
        self.assertEqual(argv[-3:], ["-f", "mp4", OUTPUT_PATH])
        self.assertIn(("-progress", "pipe:1"), pairs(argv))
        self.assertIn(("-map", "0:v:0"), pairs(argv))
        self.assertIn(("-map", "0:a?"), pairs(argv))
        self.assertIn(("-map_metadata", "0"), pairs(argv))
        self.assertIn(("-map_chapters", "0"), pairs(argv))
        self.assertIn(("-fflags", "+genpts"), pairs(argv))
        self.assertIn(("-avoid_negative_ts", "make_zero"), pairs(argv))
        self.assertLess(argv.index("-fflags"), argv.index("-i"))

    def test_identity_tag(self):
        metadata = value_after(self.argv, "-metadata")
        key, _, text = metadata.partition("=")
        self.assertEqual(key, "vidfunnel")
        tag = json.loads(text)
        self.assertEqual(sorted(tag), ["id", "profile", "src", "tool", "ver"])
        self.assertEqual(tag["id"], RECORD_ID)
        self.assertEqual(tag["tool"], "vidfunnel")
        self.assertEqual(tag["profile"], PROFILE)
        self.assertRegex(tag["src"], r"^[0-9a-f]{16}$")
        self.assertEqual(tag["src"], SOURCE_SHA[:16])
        self.assertNotIn(" ", text)


class HighDefinitionInterlacedTest(unittest.TestCase):

    def test_field_rate_needs_level_four_two(self):
        argv = argv_for("h264_1080i_tff_ts_ac3.ts")
        self.assertEqual(value_after(argv, "-level"), "4.2")
        self.assertIn("bwdif=mode=1:parity=0", value_after(argv, "-vf"))
        self.assertEqual(value_after(argv, "-crf"), "20")

    def test_max_level_four_one_deinterlaces_at_frame_rate(self):
        probe, analysis = load_pair("h264_1080i_tff_ts_ac3.ts")
        changed = copy.deepcopy(analysis)
        changed["cadence"] = decide_cadence(analysis["cadence"]["idet"], probe["video"], "4.1")
        argv = argv_for("h264_1080i_tff_ts_ac3.ts", make_options(max_level="4.1"), changed)
        self.assertEqual(value_after(argv, "-level"), "4.1")
        self.assertIn("bwdif=mode=0:parity=0", value_after(argv, "-vf"))
        self.assertEqual(value_after(argv, "-g"), "60")
        self.assertEqual(value_after(argv, "-keyint_min"), "30")


class RemuxTest(unittest.TestCase):

    def test_h264_mkv_is_remuxed(self):
        probe, analysis = load_pair("h264_high_mkv_aac.mkv")
        self.assertEqual(decide_action(probe, analysis, make_options(), {})[0], "remux")
        argv = argv_for("h264_high_mkv_aac.mkv")
        self.assertEqual(value_after(argv, "-c:v"), "copy")
        self.assertEqual(value_after(argv, "-c:a:0"), "copy")
        for absent in ("-vf", "-crf", "-preset", "-filter:a:0", "-aspect", "-g"):
            self.assertNotIn(absent, argv)
        # The analysis inferred the colour tags (240 lines at 25 fps: PAL family), so h264_metadata
        # writes them into the VUI; the SAR was not changed, so no sample_aspect_ratio.
        self.assertEqual(analysis["colour"]["result"]["source"], "inferred")
        self.assertEqual(value_after(argv, "-bsf:v"),
                         "h264_metadata=colour_primaries=5:transfer_characteristics=1:matrix_coefficients=5:"
                         "video_full_range_flag=0")

    def test_reencode_video_option_forces_libx264(self):
        probe, analysis = load_pair("h264_high_mkv_aac.mkv")
        options = make_options(reencode_video=True)
        action, reason = decide_action(probe, analysis, options, {})
        self.assertEqual((action, reason), ("reencode", "--reencode-video given"))
        argv = argv_for("h264_high_mkv_aac.mkv", options)
        self.assertEqual(value_after(argv, "-c:v"), "libx264")
        self.assertEqual(value_after(argv, "-crf"), "18")
        self.assertNotIn("-bsf:v", argv)

    def test_letterbox_sar_is_rewritten(self):
        probe, analysis = load_pair("letterbox_720x480_sar_1x1.mkv")
        self.assertEqual(analysis["aspect"]["result"]["source"], "bars")
        action, _reason = decide_action(probe, analysis, make_options(), {})
        argv = argv_for("letterbox_720x480_sar_1x1.mkv")
        if action == "remux":
            self.assertTrue(value_after(argv, "-bsf:v").startswith("h264_metadata=sample_aspect_ratio=8/9:"))
            self.assertEqual(value_after(argv, "-aspect"), "4:3")
        else:
            self.assertIn("setsar=8/9", value_after(argv, "-vf"))
        self.assertEqual(action, "remux")

    def test_letterbox_sar_on_reencode_uses_setsar(self):
        argv = argv_for("letterbox_720x480_sar_1x1.mkv", make_options(reencode_video=True))
        self.assertEqual(value_after(argv, "-vf"),
                         "setsar=8/9,setparams=range=tv:color_primaries=smpte170m:color_trc=smpte170m:"
                         "colorspace=smpte170m")
        self.assertEqual(value_after(argv, "-aspect"), "4:3")

    def test_video_filters_none_on_remux(self):
        _probe, analysis = load_pair("letterbox_720x480_sar_1x1.mkv")
        self.assertIsNone(video_filters(analysis, "remux"))


class AudioTest(unittest.TestCase):

    def test_xvid_mp3_is_copied(self):
        argv = argv_for("xvid_avi_mp3.avi")
        self.assertEqual(value_after(argv, "-c:v"), "libx264")
        self.assertEqual(value_after(argv, "-crf"), "18")
        self.assertEqual(value_after(argv, "-c:a:0"), "copy")
        self.assertNotIn("-filter:a:0", argv)

    def test_strict_aac_reencodes_mp3(self):
        probe, analysis = load_pair("xvid_avi_mp3.avi")
        strict = copy.deepcopy(analysis)
        sync_entry = analysis["sync"]["result"][0]
        strict["audio"] = [decide_audio(probe["audio"][0], sync_entry, strict_aac=True)]
        argv = argv_for("xvid_avi_mp3.avi", make_options(audio_strict_aac=True), strict)
        self.assertEqual(value_after(argv, "-c:a:0"), "aac")
        self.assertEqual(value_after(argv, "-filter:a:0"), "aresample=async=1")

    def test_drift_is_stretched(self):
        argv = argv_for("h264_mkv_aac_drift_long.mkv")
        self.assertEqual(value_after(argv, "-c:a:0"), "aac")
        filters = value_after(argv, "-filter:a:0").split(",")
        self.assertEqual(filters[0], "aresample=async=1")
        self.assertTrue(filters[1].startswith("atempo=1.012"), filters)

    def test_five_point_one_bitrate(self):
        argv = argv_for("h264_mkv_ac3_5point1.mkv")
        self.assertEqual(value_after(argv, "-c:a:0"), "aac")
        self.assertEqual(value_after(argv, "-b:a:0"), "448k")
        self.assertNotIn("-ac", argv)
        self.assertNotIn("-ar", argv)

    def test_audio_offset_option_adds_a_shifted_input(self):
        probe, analysis = load_pair("h264_mkv_aac_offset_300ms.mkv")
        shifted = copy.deepcopy(analysis)
        shifted["sync"] = decide_sync(probe, analysis["sync"]["durations"], audio_offset=0.0)
        measured = shifted["sync"]["result"][0]["measured_offset"]
        argv = argv_for("h264_mkv_aac_offset_300ms.mkv", make_options(audio_offset=0.0), shifted)
        self.assertEqual(value_after(argv, "-itsoffset"), "{:.6f}".format(0.0 - measured))
        self.assertEqual(argv.count("-i"), 2)
        self.assertIn(("-map", "1:a:0"), pairs(argv))
        self.assertNotIn(("-map", "0:a?"), pairs(argv))


class PixelArtAndSubtitleTest(unittest.TestCase):

    def test_palette_upscale_and_tune(self):
        argv = argv_for("palette_msrle_avi_160x120.avi")
        self.assertIn("scale=iw*3:ih*3:flags=neighbor", value_after(argv, "-vf").split(","))
        self.assertEqual(value_after(argv, "-tune"), "animation")

    def test_srt_to_mov_text(self):
        argv = argv_for("h264_mkv_srt_subtitles.mkv")
        self.assertIn(("-map", "0:s:0"), pairs(argv))
        self.assertEqual(value_after(argv, "-c:s"), "mov_text")
        _probe, analysis = load_pair("h264_mkv_srt_subtitles.mkv")
        sidecars = build_sidecar_argvs(FAKE_TOOLS, ["/library/clips/source.mkv"], OUTPUT_PATH, analysis)
        self.assertEqual([(entry["kind"], entry["path"]) for entry in sidecars],
                         [("srt", "/library/clips/output.srt")])
        self.assertIn(("-map", "0:s:0"), pairs(sidecars[0]["argv"]))
        self.assertIn(("-c:s", "srt"), pairs(sidecars[0]["argv"]))
        self.assertEqual(sidecars[0]["argv"][-1], "/library/clips/output.srt")

    def test_srt_sidecar_from_a_fresh_subtitle_decision(self):
        probe, analysis = load_pair("h264_mkv_srt_subtitles.mkv")
        fresh = copy.deepcopy(analysis)
        fresh["subtitles"] = decide_subtitles(probe["subtitles"])
        sidecars = build_sidecar_argvs(FAKE_TOOLS, ["/library/clips/source.mkv"], OUTPUT_PATH, fresh)
        self.assertEqual([entry["path"] for entry in sidecars], ["/library/clips/output.srt"])

    def test_several_text_streams_get_numbered_srt_sidecars(self):
        probe, analysis = load_pair("h264_mkv_srt_subtitles.mkv")
        mixed = copy.deepcopy(analysis)
        mixed["subtitles"] = decide_subtitles([{"index": 2, "codec": "subrip", "language": "eng"},
                                               {"index": 3, "codec": "ass", "language": "fra"},
                                               {"index": 4, "codec": "hdmv_pgs_subtitle", "language": None}])
        sidecars = build_sidecar_argvs(FAKE_TOOLS, ["/library/clips/source.mkv"], OUTPUT_PATH, mixed)
        self.assertEqual([(entry["kind"], entry["path"]) for entry in sidecars],
                         [("srt", "/library/clips/output.0.srt"), ("srt", "/library/clips/output.1.srt"),
                          ("sup", "/library/clips/output.sup")])
        self.assertIn(("-map", "0:s:1"), pairs(sidecars[1]["argv"]))

    def test_ass_to_mov_text_and_srt_sidecar(self):
        argv = argv_for("h264_mkv_ass_subtitles.mkv")
        self.assertEqual(value_after(argv, "-c:s"), "mov_text")
        _probe, analysis = load_pair("h264_mkv_ass_subtitles.mkv")
        sidecars = build_sidecar_argvs(FAKE_TOOLS, ["/library/clips/source.mkv"], OUTPUT_PATH, analysis)
        self.assertEqual(len(sidecars), 1)
        sidecar = sidecars[0]
        self.assertEqual(sidecar["kind"], "srt")
        self.assertEqual(sidecar["path"], "/library/clips/output.srt")
        self.assertEqual(sidecar["argv"][:4], [FAKE_TOOLS.ffmpeg, "-hide_banner", "-nostdin", "-y"])
        self.assertIn(("-map", "0:s:0"), pairs(sidecar["argv"]))
        self.assertIn(("-c:s", "srt"), pairs(sidecar["argv"]))
        self.assertEqual(sidecar["argv"][-1], "/library/clips/output.srt")

    def test_dvd_subtitles_go_to_one_matroska_sidecar(self):
        probe, analysis = load_pair("h264_mkv_srt_subtitles.mkv")
        bitmap = copy.deepcopy(analysis)
        streams = [{"index": 2, "codec": "dvd_subtitle", "language": "eng"},
                   {"index": 3, "codec": "dvd_subtitle", "language": "fra"}]
        bitmap["subtitles"] = decide_subtitles(streams)
        argv = argv_for("h264_mkv_srt_subtitles.mkv", analysis=bitmap)
        self.assertFalse([pair for pair in pairs(argv) if pair[0] == "-map" and pair[1].startswith("0:s")])
        self.assertNotIn("-c:s", argv)
        sidecars = build_sidecar_argvs(FAKE_TOOLS, ["/library/clips/source.mkv"], OUTPUT_PATH, bitmap)
        self.assertEqual([entry["kind"] for entry in sidecars], ["subs.mkv"])
        sidecar = sidecars[0]
        self.assertEqual(sidecar["path"], "/library/clips/output.subs.mkv")
        self.assertIn(("-map", "0:s:0"), pairs(sidecar["argv"]))
        self.assertIn(("-map", "0:s:1"), pairs(sidecar["argv"]))
        self.assertIn(("-c:s", "copy"), pairs(sidecar["argv"]))

    def test_pgs_goes_to_sup(self):
        _probe, analysis = load_pair("h264_mkv_srt_subtitles.mkv")
        pgs = copy.deepcopy(analysis)
        pgs["subtitles"] = decide_subtitles([{"index": 2, "codec": "hdmv_pgs_subtitle", "language": None}])
        sidecars = build_sidecar_argvs(FAKE_TOOLS, ["/library/clips/source.mkv"], OUTPUT_PATH, pgs)
        self.assertEqual(sidecars[0]["path"], "/library/clips/output.sup")
        self.assertIn(("-f", "sup"), pairs(sidecars[0]["argv"]))


class ContainerAndInputTest(unittest.TestCase):

    def test_plain_mp4(self):
        argv = argv_for("h264_high_mkv_aac.mkv", make_options(plain_mp4=True))
        self.assertEqual(value_after(argv, "-movflags"), "+faststart+use_metadata_tags")
        tag = json.loads(value_after(argv, "-metadata").partition("=")[2])
        self.assertEqual(tag["profile"], "h264-high41-mp4")

    def test_input_arguments(self):
        self.assertEqual(input_arguments(["/discs/one/VTS_01_1.VOB", "/discs/one/VTS_01_2.VOB"]),
                         ["-i", "concat:/discs/one/VTS_01_1.VOB|/discs/one/VTS_01_2.VOB"])
        self.assertEqual(input_arguments(["/discs/one/VIDEO_TS"], dvd_title=3),
                         ["-f", "dvdvideo", "-title", "3", "-i", "/discs/one/VIDEO_TS"])
        with self.assertRaises(PlanError):
            input_arguments(["/discs/odd|name/VTS_01_1.VOB", "/discs/odd|name/VTS_01_2.VOB"])

    def test_concat_argv(self):
        probe, analysis = load_pair("vob_fragments_no_ifo/VTS_01_1.VOB")
        members = ["/discs/one/VTS_01_1.VOB", "/discs/one/VTS_01_2.VOB"]
        argv = build_argv(FAKE_TOOLS, members, OUTPUT_PATH, probe, analysis, "reencode", make_options(), RECORD_ID,
                          SOURCE_SHA)
        self.assertEqual(value_after(argv, "-i"), "concat:" + "|".join(members))

    def test_crf_for_height(self):
        self.assertEqual([crf_for_height(height) for height in (576, 577, 720, 721)], [18, 19, 19, 20])

    def test_identity_tag_is_compact_json(self):
        text = identity_tag(RECORD_ID, SOURCE_SHA)
        self.assertEqual(list(json.loads(text)), ["id", "tool", "ver", "src", "profile"])
        self.assertNotIn(" ", text)


class DecideActionTest(unittest.TestCase):

    def test_swf_without_video(self):
        probe = {"format": {"name": "swf"}, "streams": [], "video": None, "audio": [], "subtitles": []}
        self.assertEqual(decide_action(probe, None, make_options(), {}), ("quarantine", "swf-vector"))

    def test_no_video(self):
        probe = {"format": {"name": "mp3"}, "streams": [{"type": "audio", "index": 0, "codec": "mp3"}],
                 "video": None, "audio": [], "subtitles": []}
        self.assertEqual(decide_action(probe, None, make_options(), {}), ("quarantine", "no-video"))

    def test_unsupported_codec(self):
        probe, analysis = load_pair("xvid_avi_mp3.avi")
        capabilities = {"decodable": {"mp3", "h264"}}
        self.assertEqual(decide_action(probe, analysis, make_options(), capabilities), ("quarantine", "unsupported"))

    def test_decode_failed_only_without_decoded_frames(self):
        probe, analysis = load_pair("xvid_avi_mp3.avi")
        broken = copy.deepcopy(analysis)
        broken["decode"]["result"]["returncode"] = 1
        self.assertEqual(decide_action(probe, broken, make_options(), {})[0], "reencode")
        for key in ("tff", "bff", "progressive", "undetermined"):
            broken["cadence"]["idet"]["totals"][key] = 0
        self.assertEqual(decide_action(probe, broken, make_options(), {}), ("quarantine", "decode-failed"))


class PlanSizeEntriesTest(unittest.TestCase):

    def test_entries_copied_from_the_analysis(self):
        analysis = {"size_class": {"bits_per_pixel": 0.1035, "class": "heavy", "reason": "words"},
                    "blockiness": {"score": 209.88, "frames": 125, "reason": "words"}}
        self.assertEqual(plan_size_entries(analysis),
                         ({"class": "heavy", "bits_per_pixel": 0.1035}, {"score": 209.88}))

    def test_analysis_without_the_entries(self):
        self.assertEqual(plan_size_entries({}), (None, None))
        self.assertEqual(plan_size_entries(None), (None, None))


# --- names and sources on a temporary tree ---------------------------------------------


class JobsOptionTest(unittest.TestCase):

    def test_parser_accepts_jobs_and_keeps_it_out_of_the_record(self):
        from vidfunnel import cli
        from vidfunnel.plan import _options_dict, options_from_args
        args = cli.build_parser().parse_args(["plan", "--jobs", "3", "somewhere"])
        self.assertEqual(args.jobs, 3)
        options = options_from_args(args)
        self.assertEqual(options.jobs, 3)
        self.assertNotIn("jobs", _options_dict(options))
        self.assertEqual(cli.build_parser().parse_args(["plan"]).jobs, 1)

    def test_parser_accepts_mark_outputs_and_records_it(self):
        from vidfunnel import cli
        from vidfunnel.plan import _options_dict, options_from_args
        options = options_from_args(cli.build_parser().parse_args(["plan", "--mark-outputs", "somewhere"]))
        self.assertTrue(options.mark_outputs)
        self.assertTrue(_options_dict(options)["mark_outputs"])
        self.assertFalse(options_from_args(cli.build_parser().parse_args(["plan", "somewhere"])).mark_outputs)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.build_parser().parse_args(["plan", "--jobs", "0"])

    def test_output_name_key(self):
        from vidfunnel.plan import _output_name_key

        class Item(object):
            def __init__(self, key, stem=None, dvd_title=None):
                self.key = Path(key)
                self.folder = self.key.parent
                self.stem = stem
                self.dvd_title = dvd_title

        self.assertEqual(_output_name_key(Item("/films/Movie.avi")), _output_name_key(Item("/films/movie.mkv")))
        self.assertNotEqual(_output_name_key(Item("/films/movie.avi")), _output_name_key(Item("/other/movie.mkv")))
        self.assertEqual(_output_name_key(Item("/films/VTS_01_1.VOB")), ("/films", "vts_01.mp4"))
        self.assertEqual(_output_name_key(Item("/films/DISC_title01", "DISC_title01", 1)),
                         _output_name_key(Item("/films/disc_title01.avi")))


def touch(path, content=b""):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


class NamesTest(unittest.TestCase):

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_same_stem(self):
        movie_avi = touch(self.root / "movie.avi")
        movie_mkv = touch(self.root / "movie.mkv")
        names = output_names(movie_avi, [movie_avi, movie_mkv])
        self.assertEqual(names, {"name": "movie.mp4", "interim_name": None, "collision": "same-stem"})
        self.assertEqual(output_names(movie_mkv, [movie_avi, movie_mkv])["collision"], "same-stem")

    def test_mp4_source_gets_interim_name(self):
        clip = touch(self.root / "clip.mp4")
        self.assertEqual(output_names(clip, [clip]),
                         {"name": "clip.mp4", "interim_name": "clip.vidfunnel.mp4", "collision": None})

    def test_lone_source(self):
        clip = touch(self.root / "clip.avi")
        self.assertEqual(output_names(clip, [clip]), {"name": "clip.mp4", "interim_name": None, "collision": None})

    def test_existing_untagged_output_is_foreign(self):
        clip = touch(self.root / "clip.avi")
        touch(self.root / "clip.mp4", b"not ours")
        self.assertEqual(output_names(clip, [clip])["collision"], "foreign-output")

    def test_marked_source_gets_the_interim_name_whatever_its_extension(self):
        clip = touch(self.root / "clip.avi")
        self.assertEqual(output_names(clip, [clip], mark=True),
                         {"name": "clip.mp4", "interim_name": "clip.vidfunnel.mp4", "collision": None})
        movie = touch(self.root / "movie.mp4")
        self.assertEqual(output_names(movie, [movie], mark=True),
                         {"name": "movie.mp4", "interim_name": "movie.vidfunnel.mp4", "collision": None})
        first = touch(self.root / "VTS_01_1.VOB")
        self.assertEqual(output_names(first, [first], mark=True)["interim_name"], "VTS_01.vidfunnel.mp4")

    def test_marked_source_meets_a_foreign_file_at_either_name(self):
        clip = touch(self.root / "clip.avi")
        touch(self.root / "clip.mp4", b"not ours")
        names = output_names(clip, [clip], mark=True)
        self.assertEqual((names["collision"], names["collision_name"]), ("foreign-output", "clip.mp4"))
        other = touch(self.root / "other.avi")
        touch(self.root / "other.vidfunnel.mp4", b"not ours either")
        names = output_names(other, [other], mark=True)
        self.assertEqual(names["collision"], "foreign-output")
        self.assertNotIn("collision_name", names)

    def test_interim_sidecars_follow_the_interim_output(self):
        from vidfunnel.plan import interim_sidecars
        sidecars = build_sidecar_argvs(FAKE_TOOLS, ["/films/Party.avi"], "/films/Party.mp4",
                                       {"subtitles": {"result": [{"action": "mov_text", "sidecar": ".srt"},
                                                                 {"action": "mov_text", "sidecar": ".srt"},
                                                                 {"sidecar": ".sup"}]}})
        renamed = interim_sidecars(sidecars, "/films/Party.mp4", "/films/Party.vidfunnel.mp4")
        self.assertEqual([entry["path"] for entry in renamed],
                         ["/films/Party.vidfunnel.0.srt", "/films/Party.vidfunnel.1.srt", "/films/Party.vidfunnel.sup"])
        for entry, original in zip(renamed, sidecars):
            self.assertEqual(entry["argv"][-1], entry["path"])
            self.assertEqual(entry["argv"][:-1], original["argv"][:-1])
            self.assertEqual(entry["kind"], original["kind"])
        with self.assertRaises(PlanError):
            interim_sidecars(sidecars, "/films/Other.mp4", "/films/Other.vidfunnel.mp4")

    def test_vob_group_named_after_title_set(self):
        first = touch(self.root / "VTS_01_1.VOB")
        second = touch(self.root / "VTS_01_2.VOB")
        self.assertEqual(output_names(first, [first, second]),
                         {"name": "VTS_01.mp4", "interim_name": None, "collision": None})

    def test_iter_sources_skips_and_groups(self):
        keep = touch(self.root / "keep.mkv")
        touch(self.root / ".vidfunnel" / "inside.avi")
        touch(self.root / ".vidfunnel-retired" / "retired.avi")
        touch(self.root / ".hidden.avi")
        touch(self.root / ".keep.mp4.part")
        touch(self.root / "partial.avi.part")
        touch(self.root / "notes.txt")
        touch(self.root / "disc" / "VTS_01_0.VOB")
        first = touch(self.root / "disc" / "VTS_01_1.VOB")
        touch(self.root / "disc" / "VTS_01_2.VOB")
        found = list(iter_sources([self.root]))
        self.assertEqual(found, [keep, first])
        self.assertEqual(list(iter_sources([self.root / "disc" / "VTS_01_2.VOB"])), [first])

    def test_iter_sources_skips_subtitle_sidecars(self):
        keep = touch(self.root / "movie.mkv")
        touch(self.root / "movie.subs.mkv")
        touch(self.root / "other.SUBS.MKV")
        self.assertEqual(list(iter_sources([self.root])), [keep])
        given = io.StringIO()
        with contextlib.redirect_stdout(given):
            self.assertEqual(list(iter_sources([self.root / "movie.subs.mkv"])), [])

    def test_iter_sources_with_fixture_vobs(self):
        source = os.path.join(FIXTURE_DIR, "vob_fragments_no_ifo")
        if not os.path.isdir(source):
            self.skipTest("fixture folder '{}' was not made (run tests/make_fixtures.sh)".format(source))
        shutil.copytree(source, str(self.root / "vobs"))
        self.assertEqual(list(iter_sources([self.root])), [self.root / "vobs" / "VTS_01_1.VOB"])


# --- integration: plan runs on fixture copies -------------------------------------------


def _tools_or_none():
    try:
        return find_tools()
    except ToolNotFound:
        return None


TOOLS = _tools_or_none()
INTEGRATION_FIXTURES = ("h264_high_mkv_aac.mkv", "xvid_avi_mp3.avi", "flv1_swf_mp3.swf", "collision_pair",
                        "vob_fragments_no_ifo")


@unittest.skipIf(TOOLS is None, "ffmpeg not found on PATH; plan integration tests skipped")
class PlanRunTest(unittest.TestCase):

    def setUp(self):
        missing = [name for name in INTEGRATION_FIXTURES if not os.path.exists(os.path.join(FIXTURE_DIR, name))]
        if missing:
            self.skipTest("fixtures missing ({}); run tests/make_fixtures.sh".format(", ".join(missing)))
        self.temporary = tempfile.TemporaryDirectory()
        # A cleanup, not tearDown, so cleanups added by a test (restoring permissions) run before it.
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "library"
        self.root.mkdir()

    def copy_fixture(self, name, destination=None):
        source = os.path.join(FIXTURE_DIR, name)
        target = self.root / (destination or name)
        if os.path.isdir(source):
            shutil.copytree(source, str(target))
        else:
            shutil.copy2(source, str(target))
        return target

    def plan(self, **values):
        return plan_paths(TOOLS, [str(self.root)], make_options(**values), progress=quiet())

    def test_every_line_of_an_item_carries_its_counter_tag(self):
        self.copy_fixture("xvid_avi_mp3.avi")
        self.copy_fixture("h264_high_mkv_aac.mkv")
        for jobs in (1, 2):
            progress = types.SimpleNamespace(stream=io.StringIO(), quiet=False)
            plan_paths(TOOLS, [str(self.root)], make_options(jobs=jobs, replan=True), progress=progress)
            text = progress.stream.getvalue()
            tags = set()
            for line in text.splitlines():
                if line.startswith("[plan "):
                    tags.add(line.split("]", 1)[0] + "]")
                else:
                    # The scan line, the summary table and the stage summary belong to no item.
                    self.assertTrue(line.startswith(("scanning ", "  ", "plan: ")), text)
            self.assertEqual(tags, {"[plan 1/2]", "[plan 2/2]"}, text)
            self.assertIn("[plan 1/2] probing '", text)
            self.assertRegex(text, r"\[plan [12]/2\] \[window pass\] ")

    def test_mark_outputs_is_recorded_and_replan_rewrites_it(self):
        source = self.copy_fixture("h264_mkv_srt_subtitles.mkv", "Movie night.mkv")
        self.plan(mark_outputs=True)
        record = load_record(source)
        self.assertEqual(record["status"], "planned")
        self.assertIs(record["stages"]["plan"]["mark_outputs"], True)
        self.assertEqual(record["output"]["name"], "Movie night.mp4")
        self.assertEqual(record["output"]["interim_name"], "Movie night.vidfunnel.mp4")
        self.assertEqual(record["output"]["sidecars"], ["Movie night.srt"])
        self.assertEqual(record["output"]["interim_sidecars"], ["Movie night.vidfunnel.srt"])
        self.assertEqual(record["stages"]["plan"]["argv"][-1], str(self.root / "Movie night.vidfunnel.mp4"))
        self.assertEqual([entry["path"] for entry in record["stages"]["plan"]["sidecars"]],
                         [str(self.root / "Movie night.vidfunnel.srt")])
        self.assertEqual(record["stages"]["plan"]["sidecars"][0]["argv"][-1],
                         str(self.root / "Movie night.vidfunnel.srt"))

        self.plan(replan=True)
        record = load_record(source)
        self.assertIs(record["stages"]["plan"]["mark_outputs"], False)
        self.assertIsNone(record["output"]["interim_name"])
        self.assertNotIn("interim_sidecars", record["output"])
        self.assertEqual(record["stages"]["plan"]["argv"][-1], str(self.root / "Movie night.mp4"))

        self.plan(replan=True, mark_outputs=True)
        record = load_record(source)
        self.assertIs(record["stages"]["plan"]["mark_outputs"], True)
        self.assertEqual(record["output"]["interim_name"], "Movie night.vidfunnel.mp4")

    def test_marked_replan_after_convert_does_not_collide_with_its_own_interim_srt(self):
        from vidfunnel.encode import convert_paths
        source = self.copy_fixture("h264_mkv_srt_subtitles.mkv", "Movie night.mkv")
        self.plan(mark_outputs=True)
        options = types.SimpleNamespace(par2=None, verbose=False, jobs=1, dry_run=False)
        progress = types.SimpleNamespace(stream=io.StringIO(), quiet=True, verbose=False)
        convert_paths(TOOLS, [str(self.root)], options, progress)
        self.assertTrue((self.root / "Movie night.vidfunnel.srt").exists())
        summary = self.plan(replan=True, mark_outputs=True)
        self.assertEqual(summary.counts["quarantine"], 0, summary.counts)
        record = load_record(source)
        self.assertEqual(record["status"], "planned")
        self.assertEqual(record["output"]["interim_sidecars"], ["Movie night.vidfunnel.srt"])
        self.assertNotIn("sidecar-exists", record["flags"])

    def test_marked_plan_leaves_the_users_own_srt_alone(self):
        source = self.copy_fixture("h264_mkv_srt_subtitles.mkv", "Movie night.mkv")
        (self.root / "Movie night.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nthe user's own\n")
        self.plan(mark_outputs=True)
        record = load_record(source)
        self.assertEqual(record["status"], "planned")
        self.assertIn("sidecar-exists", record["flags"])
        self.assertEqual(record["output"]["sidecars"], [])
        self.assertEqual(record["output"]["interim_sidecars"], [])
        self.assertEqual(record["stages"]["plan"]["sidecars"], [])

    def test_marked_plan_quarantines_a_foreign_file_at_the_final_name(self):
        source = self.copy_fixture("h264_high_mkv_aac.mkv")
        (self.root / "h264_high_mkv_aac.mp4").write_bytes(b"someone else's file")
        # Planned by its own path: a folder run would plan the foreign .mp4 as a source of the same stem.
        plan_paths(TOOLS, [str(source)], make_options(mark_outputs=True), progress=quiet())
        record = load_record(source)
        self.assertEqual((record["status"], record["quarantine_reason"]), ("quarantined", "output-collision"))
        self.assertIn("'h264_high_mkv_aac.mp4' already exists", record["stages"]["plan"]["reason"])

    def test_users_own_srt_beside_the_source_is_left_alone(self):
        source = self.copy_fixture("h264_mkv_srt_subtitles.mkv", "Movie night.mkv")
        own = self.root / "Movie night.srt"
        own.write_text("1\n00:00:01,000 --> 00:00:02,000\nthe user's own subtitles\n")
        summary = self.plan()
        self.assertEqual(summary.counts["quarantine"], 0, summary.counts)
        record = load_record(source)
        self.assertEqual(record["status"], "planned")
        self.assertIn("sidecar-exists", record["flags"])
        self.assertEqual(record["output"]["sidecars"], [])
        self.assertEqual(record["stages"]["plan"]["sidecars"], [])
        self.assertEqual(record["stages"]["plan"]["sidecars_skipped"], ["Movie night.srt"])
        self.assertIn("the user's own subtitles", own.read_text())

    def test_replan_after_convert_does_not_collide_with_its_own_srt(self):
        from vidfunnel.encode import convert_paths
        source = self.copy_fixture("h264_mkv_srt_subtitles.mkv", "Movie night.mkv")
        self.plan()
        options = types.SimpleNamespace(par2=None, verbose=False, jobs=1, dry_run=False)
        progress = types.SimpleNamespace(stream=io.StringIO(), quiet=True, verbose=False)
        convert_paths(TOOLS, [str(self.root)], options, progress)
        self.assertTrue((self.root / "Movie night.srt").exists())
        summary = self.plan(replan=True)
        self.assertEqual(summary.counts["quarantine"], 0, summary.counts)
        record = load_record(source)
        self.assertEqual(record["status"], "planned")
        self.assertEqual(record["output"]["sidecars"], ["Movie night.srt"])
        self.assertNotIn("sidecar-exists", record["flags"])

    def test_records_statuses_and_skip(self):
        for name in INTEGRATION_FIXTURES:
            self.copy_fixture(name)
        summary = self.plan()
        self.assertEqual(summary.counts, {"remux": 1, "re-encode": 3, "quarantine": 2, "skipped": 0, "failed": 0})

        expected = {
            "h264_high_mkv_aac.mkv": ("planned", "remux"),
            "xvid_avi_mp3.avi": ("planned", "reencode"),
            "flv1_swf_mp3.swf": ("planned", "reencode"),
            "collision_pair/movie.avi": ("quarantined", "quarantine"),
            "collision_pair/movie.mkv": ("quarantined", "quarantine"),
            "vob_fragments_no_ifo/VTS_01_1.VOB": ("planned", "reencode"),
        }
        for relative, (status, action) in expected.items():
            record = load_record(self.root / relative)
            self.assertIsNotNone(record, relative)
            self.assertEqual((record["status"], record["action"]), (status, action), relative)
            self.assertRegex(record["source"]["sha256"], r"^[0-9a-f]{64}$")
            if status == "planned":
                argv = record["stages"]["plan"]["argv"]
                self.assertEqual(argv[0], TOOLS.ffmpeg)
                self.assertEqual(argv[-1], str(self.root / Path(relative).parent / record["output"]["name"]))
                tag = json.loads(value_after(argv, "-metadata").partition("=")[2])
                self.assertEqual(tag["src"], record["source"]["sha256"][:16])
            else:
                self.assertEqual(record["quarantine_reason"], "output-collision")
                self.assertIn("output-collision", record["flags"])
                self.assertIsNone(record["stages"]["plan"]["argv"])
                self.assertEqual(record["stages"]["plan"]["collision"], "same-stem")
        self.assertFalse(record_path(self.root / "vob_fragments_no_ifo" / "VTS_01_2.VOB").exists())
        group = load_record(self.root / "vob_fragments_no_ifo" / "VTS_01_1.VOB")
        self.assertEqual(group["output"]["name"], "VTS_01.mp4")
        self.assertIn("vob-no-ifo", group["flags"])
        self.assertEqual(group["stages"]["analyze"]["decode"]["result"]["returncode"], 0)
        self.assertIn("concat:", value_after(group["stages"]["plan"]["argv"], "-i"))
        self.assertEqual([member["name"] for member in group["source"]["members"]], ["VTS_01_1.VOB", "VTS_01_2.VOB"])
        self.assertEqual(load_record(self.root / "h264_high_mkv_aac.mkv")["stages"]["plan"]["keyframes"]["keyframes"]
                         > 0, True)
        self.assertNotIn("raw", load_record(self.root / "xvid_avi_mp3.avi")["stages"]["probe"])
        for media in self.root.rglob("*.mp4"):
            self.fail("plan wrote media: '{}'".format(media))

        again = self.plan()
        self.assertEqual(again.counts, {"remux": 0, "re-encode": 0, "quarantine": 0, "skipped": 6, "failed": 0})

    def plan_one(self, name):
        return plan_paths(TOOLS, [str(self.root / name)], make_options(), progress=quiet())

    def copy_collision_pair(self):
        self.copy_fixture(os.path.join("collision_pair", "movie.avi"), "movie.avi")
        self.copy_fixture(os.path.join("collision_pair", "movie.mkv"), "movie.mkv")

    def test_same_stem_planned_in_separate_runs_quarantines_both(self):
        self.copy_collision_pair()
        self.plan_one("movie.avi")
        first = load_record(self.root / "movie.avi")
        self.assertEqual(first["status"], "planned")
        self.assertEqual(first["output"]["name"], "movie.mp4")
        summary = self.plan_one("movie.mkv")
        self.assertEqual(summary.counts["quarantine"], 1)
        second = load_record(self.root / "movie.mkv")
        self.assertEqual((second["status"], second["quarantine_reason"]), ("quarantined", "output-collision"))
        self.assertEqual(second["stages"]["plan"]["collision"], "same-stem")
        self.assertIn("'movie.avi'", second["stages"]["plan"]["reason"])
        first = load_record(self.root / "movie.avi")
        self.assertEqual((first["status"], first["quarantine_reason"]), ("quarantined", "output-collision"))
        self.assertIn("output-collision", first["flags"])
        self.assertEqual(first["stages"]["plan"]["collision"], "same-stem")
        self.assertIn("'movie.mkv'", first["stages"]["plan"]["collision_reason"])

    def test_same_stem_against_a_converted_record_quarantines_only_the_new_source(self):
        self.copy_collision_pair()
        self.plan_one("movie.avi")
        first = load_record(self.root / "movie.avi")
        set_status(first, "converted")
        save_record(self.root / "movie.avi", first)
        self.plan_one("movie.mkv")
        second = load_record(self.root / "movie.mkv")
        self.assertEqual((second["status"], second["quarantine_reason"]), ("quarantined", "output-collision"))
        self.assertIn("'movie.mp4' is already the output of 'movie.avi'", second["stages"]["plan"]["reason"])
        self.assertEqual(load_record(self.root / "movie.avi"), first)

    # --- plan --jobs ---------------------------------------------------------------------

    VOLATILE_KEYS = ("id", "created", "at", "status_at", "replanned_at", "history")

    def comparable_records(self, root):
        """{relative source key: record} with ids, timestamps and history removed and the root and ids masked."""
        found = {}
        for folder, record in iter_records(root):
            ids = [record["id"]]
            text = json.dumps(record, sort_keys=True).replace(str(root), "<root>")
            for record_id in ids:
                text = text.replace(record_id, "<id>")

            def strip(value):
                if isinstance(value, dict):
                    return {key: strip(item) for key, item in value.items() if key not in self.VOLATILE_KEYS}
                if isinstance(value, list):
                    return [strip(item) for item in value]
                return value

            relative = os.path.relpath(str(folder), str(root))
            found[os.path.join(relative, record["source"]["name"])] = strip(json.loads(text))
        return found

    def plan_twice(self, layout):
        """Plan the same sources with jobs=1 and jobs=3 in two copies; (records and counts of each)."""
        results = []
        for jobs in (1, 3):
            self.root = Path(self.temporary.name) / "library_jobs_{}".format(jobs)
            self.root.mkdir()
            for name, destination in layout:
                self.copy_fixture(name, destination)
            progress = quiet()
            summary = plan_paths(TOOLS, [str(self.root)], make_options(jobs=jobs), progress=progress)
            results.append((self.comparable_records(self.root), summary.counts, progress.stream.getvalue()))
        return results

    def assert_parity(self, results):
        (sequential, sequential_counts, _text), (parallel, parallel_counts, parallel_text) = results
        self.assertEqual(parallel_counts, sequential_counts, parallel_text)
        self.assertEqual(sorted(parallel), sorted(sequential))
        for key in sorted(sequential):
            self.assertEqual(parallel[key], sequential[key], key)

    def test_jobs_three_equals_jobs_one_in_one_flat_folder(self):
        layout = [("h264_high_mkv_aac.mkv", None), ("xvid_avi_mp3.avi", None), ("flv1_swf_mp3.swf", None),
                  (os.path.join("collision_pair", "movie.avi"), "movie.avi"),
                  (os.path.join("collision_pair", "movie.mkv"), "movie.mkv"),
                  (os.path.join("vob_fragments_no_ifo", "VTS_01_1.VOB"), "VTS_01_1.VOB"),
                  (os.path.join("vob_fragments_no_ifo", "VTS_01_2.VOB"), "VTS_01_2.VOB")]
        results = self.plan_twice(layout)
        self.assert_parity(results)
        records, counts, _text = results[1]
        self.assertEqual(counts, {"remux": 1, "re-encode": 3, "quarantine": 2, "skipped": 0, "failed": 0})
        for name in ("movie.avi", "movie.mkv"):
            record = records[os.path.join(".", name)]
            self.assertEqual((record["status"], record["quarantine_reason"]), ("quarantined", "output-collision"))
        self.assertIn("[plan 6/6]", results[1][2])

    def test_jobs_three_equals_jobs_one_across_folders(self):
        results = self.plan_twice([(name, None) for name in INTEGRATION_FIXTURES])
        self.assert_parity(results)

    def test_same_stem_items_planned_at_once_both_quarantine(self):
        from vidfunnel.ffmpeg import capabilities as ffmpeg_capabilities
        from vidfunnel.plan import _Work, _plan_one
        from vidfunnel.probe import decodable_codecs
        self.copy_collision_pair()
        sources = [self.root / "movie.avi", self.root / "movie.mkv"]
        capabilities = dict(ffmpeg_capabilities(TOOLS))
        capabilities["decodable"] = decodable_codecs(TOOLS)
        record_lock = threading.Lock()
        outcomes = {}

        def plan_source(source):
            outcomes[source.name] = _plan_one(TOOLS, _Work(source), make_options(), sources, capabilities, quiet(),
                                              None, record_lock)[0]

        import vidfunnel.plan
        from unittest import mock
        unlocked_calls = []
        real_claims = vidfunnel.plan.recorded_claims
        real_save = vidfunnel.plan.save_record

        def checked(real, name):
            def call(*arguments, **keywords):
                if not record_lock.locked():
                    unlocked_calls.append(name)
                return real(*arguments, **keywords)
            return call

        threads = [threading.Thread(target=plan_source, args=(source,)) for source in sources]
        with mock.patch("vidfunnel.plan.recorded_claims", side_effect=checked(real_claims, "recorded_claims")), \
                mock.patch("vidfunnel.plan.save_record", side_effect=checked(real_save, "save_record")):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(outcomes, {"movie.avi": "quarantine", "movie.mkv": "quarantine"})
        self.assertEqual(unlocked_calls, [], "the record step ran without the folder's lock")
        self.assertFalse(record_lock.locked())
        for source in sources:
            record = load_record(source)
            self.assertEqual((record["status"], record["quarantine_reason"]), ("quarantined", "output-collision"))
            self.assertEqual(record["stages"]["plan"]["collision"], "same-stem")

    def test_jobs_puts_items_with_one_output_name_on_one_worker_in_order(self):
        self.copy_collision_pair()
        self.copy_fixture("xvid_avi_mp3.avi")
        self.copy_fixture("h264_high_mkv_aac.mkv")
        seen = []

        def fake_entry(tools, work, found, options, folder_sources, capabilities, progress, stream, position, total,
                       record_lock=None):
            seen.append((work.source.name, threading.get_ident(), time.monotonic()))
            if work.source.name == "movie.avi":
                time.sleep(0.3)
            return "failed", None

        from unittest import mock
        with mock.patch("vidfunnel.plan._plan_entry", side_effect=fake_entry):
            plan_paths(TOOLS, [str(self.root)], make_options(jobs=3), progress=quiet())
        by_name = {name: (thread, moment) for name, thread, moment in seen}
        self.assertEqual(sorted(by_name), ["h264_high_mkv_aac.mkv", "movie.avi", "movie.mkv", "xvid_avi_mp3.avi"])
        self.assertEqual(by_name["movie.avi"][0], by_name["movie.mkv"][0])
        self.assertGreaterEqual(by_name["movie.mkv"][1] - by_name["movie.avi"][1], 0.3)

    def test_jobs_lock_a_folder_when_its_first_group_starts_and_skip_a_held_one(self):
        import datetime
        from unittest import mock
        import vidfunnel.plan
        import vidfunnel.record
        import vidfunnel.turns
        from vidfunnel.record import new_record
        names = ("first", "second", "third")
        for folder, stem in (("first", "harbour"), ("first", "jetty"), ("second", "meadow"), ("third", "valley")):
            (self.root / folder).mkdir(exist_ok=True)
            self.copy_fixture("xvid_avi_mp3.avi", "{}/{}.avi".format(folder, stem))
        held_record = new_record(self.root / "second" / "meadow.avi")
        save_record(self.root / "second" / "meadow.avi", held_record)
        held_bytes = record_path(self.root / "second" / "meadow.avi").read_bytes()
        foreign = self.root / "second" / ".vidfunnel" / "lock"
        started = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        foreign.write_text(json.dumps({"host": "elsewhere-host", "pid": 4242, "started": started, "container": False,
                                       "machine": "elsewhere-machine", "machine_source": "machine-id",
                                       "boot": "elsewhere-boot", "flock": False,
                                       "heartbeat": {"interval": vidfunnel.record.HEARTBEAT_SECONDS}}),
                           encoding="utf-8")
        foreign_bytes = foreign.read_bytes()

        def lock_of(name):
            return self.root / name / ".vidfunnel" / "lock"

        def ours(name):
            try:
                return json.loads(lock_of(name).read_text(encoding="utf-8")).get("pid") == os.getpid()
            except (OSError, ValueError):
                return False

        entered = []
        real_enter = vidfunnel.turns.FolderTurns.enter

        def enter(turns, folder):
            entered.append(Path(folder).name)
            return real_enter(turns, folder)

        both_first = threading.Barrier(2, timeout=60)
        both_looked = threading.Barrier(2, timeout=60)
        seen_in_first = []
        planned = []

        def fake_entry(tools, work, found, options, folder_sources, capabilities, progress, stream, position, total,
                       record_lock=None):
            folder = work.source.parent.name
            planned.append(work.source.name)
            if folder == "first":
                both_first.wait()  # both workers are in the first folder: no other folder's group has started
                seen_in_first.append({name: ours(name) for name in names})
                both_looked.wait()  # neither group ends, so no worker moves on, before both have looked
            return "failed", None

        updated = []
        real_update = vidfunnel.plan._update_folder_file

        def update(folder, works):
            updated.append((Path(folder).name, ours(Path(folder).name)))
            return real_update(folder, works)

        stop = threading.Event()
        strays = []

        def sample():
            while not stop.is_set():
                present = [name for name in names if name != "second" and lock_of(name).exists()]
                after = set(entered)
                strays.extend(name for name in present if name not in after)
                stop.wait(0.001)

        sampler = threading.Thread(target=sample, daemon=True)
        progress = quiet()
        with mock.patch("vidfunnel.plan._plan_entry", side_effect=fake_entry), \
                mock.patch("vidfunnel.plan._update_folder_file", side_effect=update), \
                mock.patch.object(vidfunnel.turns.FolderTurns, "enter", enter):
            sampler.start()
            try:
                summary = plan_paths(TOOLS, [str(self.root)], make_options(jobs=2), progress=progress)
            finally:
                stop.set()
                sampler.join(60)
        text = progress.stream.getvalue()
        self.assertEqual(seen_in_first, [{"first": True, "second": False, "third": False}] * 2, text)
        self.assertEqual(strays, [], "a lock file appeared before any group of its folder started")
        self.assertEqual(sorted(planned), ["harbour.avi", "jetty.avi", "valley.avi"])
        self.assertEqual(text.count("skipping folder '{}': ".format(self.root / "second")), 1, text)
        self.assertEqual(sorted(updated), [("first", True), ("third", True)])
        self.assertFalse((self.root / "second" / ".vidfunnel" / "folder.json").exists())
        self.assertEqual(record_path(self.root / "second" / "meadow.avi").read_bytes(), held_bytes)
        self.assertEqual(foreign.read_bytes(), foreign_bytes)
        self.assertEqual((summary.counts["skipped"], summary.counts["failed"]), (1, 3), text)
        self.assertFalse(lock_of("first").exists() or lock_of("third").exists())

    def test_jobs_a_drain_before_a_folder_is_entered_leaves_its_groups_unplanned_and_counted_left(self):
        from unittest import mock
        import vidfunnel.interrupt
        import vidfunnel.plan
        import vidfunnel.turns
        self.addCleanup(vidfunnel.interrupt.drain_tally.reset)
        self.addCleanup(vidfunnel.interrupt.draining.clear)
        real_enter = vidfunnel.turns.FolderTurns.enter
        real_lock = vidfunnel.turns.FolderLock
        acquired = []

        class LoggedLock(real_lock):
            def acquire(self):
                acquired.append(Path(self.folder).name)
                return super().acquire()

        def enter(turns, folder):
            # The group was handed out before the drain; the drain begins before its folder is entered.
            if Path(folder).name == "second":
                vidfunnel.interrupt.draining.set()
            return real_enter(turns, folder)

        planned = []

        def fake_entry(tools, work, found, options, folder_sources, capabilities, progress, stream, position, total,
                       record_lock=None):
            planned.append(work.source.name)
            return "failed", None

        for jobs in (2, 3):
            with self.subTest(jobs=jobs):
                self.root = Path(self.temporary.name) / "drained_jobs_{}".format(jobs)
                self.root.mkdir()
                for folder in ("first", "second"):
                    (self.root / folder).mkdir()
                self.copy_fixture("xvid_avi_mp3.avi", "first/harbour.avi")
                self.copy_fixture("xvid_avi_mp3.avi", "first/jetty.avi")
                # The second folder holds two groups: meadow alone, and the movie pair sharing one output name.
                self.copy_fixture("xvid_avi_mp3.avi", "second/meadow.avi")
                self.copy_fixture(os.path.join("collision_pair", "movie.avi"), "second/movie.avi")
                self.copy_fixture(os.path.join("collision_pair", "movie.mkv"), "second/movie.mkv")
                vidfunnel.interrupt.draining.clear()
                vidfunnel.interrupt.drain_tally.reset()
                acquired.clear()
                planned.clear()
                progress = quiet()
                with mock.patch("vidfunnel.plan._plan_entry", side_effect=fake_entry), \
                        mock.patch.object(vidfunnel.turns.FolderTurns, "enter", enter), \
                        mock.patch("vidfunnel.turns.FolderLock", LoggedLock):
                    summary = plan_paths(TOOLS, [str(self.root)], make_options(jobs=jobs), progress=progress)
                text = progress.stream.getvalue()
                tally = vidfunnel.interrupt.drain_tally
                self.assertTrue(vidfunnel.interrupt.draining.is_set())
                self.assertEqual(sorted(planned), ["harbour.avi", "jetty.avi"], text)
                self.assertEqual(acquired, ["first"], text)
                self.assertEqual((summary.counts["failed"], summary.counts["skipped"]), (2, 0), text)
                self.assertTrue(tally.seen)
                self.assertEqual(tally.left, 3, "the second folder's three files are left: {}".format(text))
                self.assertFalse((self.root / "second" / ".vidfunnel" / "lock").exists())
                self.assertFalse((self.root / "second" / ".vidfunnel" / "folder.json").exists())
                for name in ("meadow.avi", "movie.avi", "movie.mkv"):
                    self.assertFalse(record_path(self.root / "second" / name).exists(), name)
                self.assertNotIn("skipping folder", text)

    def test_read_only_folder_is_skipped_without_a_traceback(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("running as root; a read-only folder is still writable")
        self.copy_fixture("h264_high_mkv_aac.mkv")
        self.plan()
        for folder in (self.root / ".vidfunnel", self.root):
            os.chmod(str(folder), 0o555)
            self.addCleanup(os.chmod, str(folder), 0o755)
        progress = quiet()
        summary = plan_paths(TOOLS, [str(self.root)], make_options(), progress=progress)
        self.assertEqual(summary.counts["skipped"], 1, progress.stream.getvalue())
        self.assertEqual(summary.lock_errors, [str(self.root)])
        self.assertIn("cannot take its lock", progress.stream.getvalue())

    def test_changed_source_and_replan(self):
        clip = self.copy_fixture("xvid_avi_mp3.avi")
        self.plan()
        first = load_record(clip)
        with open(str(clip), "ab") as handle:
            handle.write(b"\0")
        summary = self.plan()
        self.assertEqual(summary.counts["re-encode"], 1)
        second = load_record(clip)
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(len(second["history"]), 1)
        self.assertEqual(second["history"][0]["id"], first["id"])
        self.assertEqual(second["history"][0]["status"], "source-changed")
        self.assertNotEqual(second["source"]["sha256"], first["source"]["sha256"])

        self.assertEqual(self.plan().counts["skipped"], 1)
        summary = self.plan(replan=True)
        self.assertEqual(summary.counts["re-encode"], 1)
        third = load_record(clip)
        self.assertNotEqual(third["id"], second["id"])
        self.assertEqual([entry["id"] for entry in third["history"]], [first["id"], second["id"]])
        self.assertEqual(third["history"][-1]["status"], "planned")
        self.assertEqual(third["source"]["sha256"], second["source"]["sha256"])

    def planned_at_a_known_time(self, name):
        clip = self.copy_fixture(name)
        os.utime(str(clip), ns=(PLANNED_MTIME_NS, PLANNED_MTIME_NS))
        self.plan()
        self.assertEqual(load_record(clip)["source"]["fingerprint"]["mtime_ns"], PLANNED_MTIME_NS)
        return clip

    def recorded_at(self, clip, recorded_ns):
        """Edit the record's recorded time, as if another mount had planned it; the record's bytes after."""
        record = load_record(clip)
        record["source"]["fingerprint"]["mtime_ns"] = recorded_ns
        save_record(clip, record)
        return record_path(clip).read_bytes()

    def plan_watching_the_hash(self, **values):
        from unittest import mock
        import vidfunnel.plan as plan_module
        import vidfunnel.record as record_module
        with mock.patch("vidfunnel.record.full_sha256", wraps=record_module.full_sha256) as checker, \
                mock.patch.object(plan_module, "_sha256_paths", wraps=plan_module._sha256_paths) as planner:
            stream = io.StringIO()
            summary = plan_paths(TOOLS, [str(self.root)], make_options(**values),
                                 progress=types.SimpleNamespace(stream=stream, quiet=False))
        return summary, checker, planner, stream.getvalue()

    def test_a_coarser_view_of_the_time_is_skipped_without_a_read(self):
        clip = self.planned_at_a_known_time(LARGE_FIXTURE)
        for word, recorded_ns, seen_ns in COARSER_VIEWS:
            with self.subTest(case=word):
                record_bytes = self.recorded_at(clip, recorded_ns)
                os.utime(str(clip), ns=(seen_ns, seen_ns))
                summary, checker, _planner, text = self.plan_watching_the_hash()
                self.assertEqual(summary.counts["skipped"], 1, text)
                checker.assert_not_called()
                self.assertEqual(record_path(clip).read_bytes(), record_bytes)

    def test_another_time_with_the_same_content_is_read_once_and_skipped(self):
        clip = self.planned_at_a_known_time(LARGE_FIXTURE)
        for word, recorded_ns, seen_ns in OTHER_TIMES:
            with self.subTest(case=word):
                record_bytes = self.recorded_at(clip, recorded_ns)
                os.utime(str(clip), ns=(seen_ns, seen_ns))
                summary, checker, _planner, text = self.plan_watching_the_hash()
                self.assertEqual(summary.counts["skipped"], 1, text)
                self.assertEqual(checker.call_count, 1)
                self.assertIn("reading '{}' in full".format(LARGE_FIXTURE), text)
                self.assertEqual(record_path(clip).read_bytes(), record_bytes)

    def test_another_time_with_a_changed_middle_is_planned_again(self):
        clip = self.planned_at_a_known_time(LARGE_FIXTURE)
        for word, recorded_ns, seen_ns in OTHER_TIMES:
            with self.subTest(case=word):
                self.recorded_at(clip, recorded_ns)
                before = load_record(clip)
                flip_middle(clip)
                os.utime(str(clip), ns=(seen_ns, seen_ns))
                summary, checker, planner, text = self.plan_watching_the_hash()
                self.assertEqual(summary.counts["skipped"], 0, text)
                self.assertEqual(checker.call_count, 1)
                planner.assert_not_called()     # the hash of the check serves the new record
                after = load_record(clip)
                self.assertNotEqual(after["id"], before["id"])
                with open(str(clip), "rb") as handle:
                    self.assertEqual(after["source"]["sha256"], hashlib.sha256(handle.read()).hexdigest())
                self.assertNotEqual(after["source"]["sha256"], before["source"]["sha256"])
                self.assertEqual(after["history"][-1]["id"], before["id"])
                self.assertEqual(after["history"][-1]["status"], "source-changed")
                self.assertIn("'{}' changed since record {} was planned (SHA-256 of".format(
                    LARGE_FIXTURE, before["id"]), text)

    def test_replan_with_a_changed_middle_marks_the_history_source_changed(self):
        clip = self.planned_at_a_known_time(LARGE_FIXTURE)
        first = load_record(clip)
        flip_middle(clip)
        os.utime(str(clip), ns=(PLANNED_MTIME_NS + SECOND_NS, PLANNED_MTIME_NS + SECOND_NS))
        summary, checker, planner, text = self.plan_watching_the_hash(replan=True)
        checker.assert_not_called()
        self.assertEqual(planner.call_count, 1)
        second = load_record(clip)
        self.assertNotEqual(second["source"]["sha256"], first["source"]["sha256"])
        self.assertEqual(second["history"][-1]["id"], first["id"])
        self.assertEqual(second["history"][-1]["status"], "source-changed")

    def test_another_time_without_a_recorded_hash_is_planned_again(self):
        clip = self.planned_at_a_known_time(LARGE_FIXTURE)
        record = load_record(clip)
        record["source"]["sha256"] = None
        save_record(clip, record)
        os.utime(str(clip), ns=(PLANNED_MTIME_NS + SECOND_NS, PLANNED_MTIME_NS + SECOND_NS))
        summary, checker, planner, text = self.plan_watching_the_hash()
        self.assertEqual(summary.counts["skipped"], 0, text)
        checker.assert_not_called()
        self.assertEqual(planner.call_count, 1)
        self.assertIn("no source SHA-256", text)
        self.assertNotEqual(load_record(clip)["id"], record["id"])

    def test_replan_after_a_time_change_hashes_the_source_again(self):
        clip = self.planned_at_a_known_time("xvid_avi_mp3.avi")
        first = load_record(clip)
        whole_seconds = truncated(PLANNED_MTIME_NS, SECOND_NS)
        os.utime(str(clip), ns=(whole_seconds, whole_seconds))
        _summary, _checker, planner, _text = self.plan_watching_the_hash(replan=True)
        planner.assert_not_called()     # a coarser view: the recorded hash is reused
        second = load_record(clip)
        self.assertEqual(second["source"]["sha256"], first["source"]["sha256"])
        days_later = OTHER_TIMES[-1][2]
        os.utime(str(clip), ns=(days_later, days_later))
        _summary, _checker, planner, _text = self.plan_watching_the_hash(replan=True)
        self.assertEqual(planner.call_count, 1)
        third = load_record(clip)
        self.assertEqual(third["source"]["sha256"], first["source"]["sha256"])
        self.assertEqual(third["source"]["fingerprint"]["mtime_ns"], days_later)
        self.assertEqual(third["history"][-1]["id"], second["id"])
        self.assertEqual(third["history"][-1]["status"], "planned")
        self.assertIn("replanned_at", third["history"][-1])

    def test_a_record_left_source_changed_by_an_older_convert_is_planned_again(self):
        # Before 20261109.3 convert set this status and flag when the source's time alone differed, as
        # another mount's coarser view does; plain plan plans such a record again, as it did then.
        clip = self.planned_at_a_known_time("xvid_avi_mp3.avi")
        record = load_record(clip)
        set_status(record, "source-changed")
        add_flag(record, "source-changed")
        save_record(clip, record)
        whole_seconds = truncated(PLANNED_MTIME_NS, SECOND_NS)
        os.utime(str(clip), ns=(whole_seconds, whole_seconds))
        summary, _checker, _planner, text = self.plan_watching_the_hash()
        self.assertEqual(summary.counts["re-encode"], 1, text)
        self.assertIn("record {} of 'xvid_avi_mp3.avi' has status 'source-changed'; re-planning under a new "
                      "record".format(record["id"]), text)
        replanned = load_record(clip)
        self.assertEqual(replanned["status"], "planned")
        self.assertNotIn("source-changed", replanned["flags"])
        self.assertEqual(replanned["source"]["sha256"], record["source"]["sha256"])
        self.assertEqual(replanned["history"][-1]["id"], record["id"])
        self.assertEqual(replanned["history"][-1]["status"], "source-changed")
        self.assertEqual(self.plan().counts["skipped"], 1)

    def test_reencode_output_carries_colour_and_aspect(self):
        source = self.copy_fixture("mpeg2_ps_ntsc_dar_4x3_ac3.mpg")
        self.plan()
        record = load_record(source)
        self.assertEqual(record["action"], "reencode")
        argv = record["stages"]["plan"]["argv"]
        run_tool(argv)
        completed = subprocess.run([TOOLS.ffprobe, "-v", "error", "-print_format", "json", "-show_streams", argv[-1]],
                                   check=True, stdout=subprocess.PIPE, universal_newlines=True)
        video = [stream for stream in json.loads(completed.stdout)["streams"] if stream["codec_type"] == "video"][0]
        self.assertEqual((video["codec_name"], video["profile"], video["pix_fmt"]), ("h264", "High", "yuv420p"))
        self.assertEqual((video.get("color_range"), video.get("color_space"), video.get("color_primaries"),
                          video.get("color_transfer")), ("tv", "smpte170m", "smpte170m", "smpte170m"))
        self.assertEqual((video.get("sample_aspect_ratio"), video.get("display_aspect_ratio")), ("8:9", "4:3"))

    def test_metadata_tag_round_trip_on_remux(self):
        titled = self.root / "titled_clip.mkv"
        subprocess.run([TOOLS.ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i",
                        os.path.join(FIXTURE_DIR, "h264_high_mkv_aac.mkv"), "-c", "copy", "-metadata",
                        "title=Round Trip Title", str(titled)], check=True)
        self.plan()
        record = load_record(titled)
        self.assertEqual(record["action"], "remux")
        argv = record["stages"]["plan"]["argv"]
        output = Path(argv[-1])
        run_tool(argv)
        self.assertTrue(output.is_file())
        completed = subprocess.run([TOOLS.ffprobe, "-v", "error", "-print_format", "json", "-show_format",
                                    "-show_streams", str(output)], check=True, stdout=subprocess.PIPE,
                                   universal_newlines=True)
        probed = json.loads(completed.stdout)
        tags = probed["format"]["tags"]
        tag = json.loads(tags["vidfunnel"])
        self.assertEqual(tag["id"], record["id"])
        self.assertEqual(tag["src"], record["source"]["sha256"][:16])
        self.assertEqual(tags.get("title"), "Round Trip Title")
        video = [stream for stream in probed["streams"] if stream["codec_type"] == "video"][0]
        self.assertEqual(video["codec_name"], "h264")

        summary = self.plan()
        self.assertEqual(summary.counts["skipped"], 1)
        self.assertEqual(summary.counts["remux"] + summary.counts["re-encode"], 0)
        self.assertFalse(record_path(output).exists())
        self.assertEqual(list(iter_sources([str(self.root)], TOOLS)), [titled])


if __name__ == "__main__":
    unittest.main()

# End of File #
