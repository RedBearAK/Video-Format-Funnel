"""Tests for vidfunnel.plan: argv building from captured probe and analysis JSON, names, sources, and plan runs.

The pure tests read tests/probe_json and tests/analysis_json and need no
ffmpeg. The integration tests copy fixtures from tests/fixtures into a
temporary folder (plan writes '.vidfunnel/' beside its sources, so the
fixture folder itself is never planned) and skip with a message when
ffmpeg or the fixtures are missing.
"""

VERSION = "20261102"

import copy
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
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
from vidfunnel.record import load_record, record_path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(REPO_ROOT, "tests", "fixtures")
PROBE_DIR = os.path.join(REPO_ROOT, "tests", "probe_json")
ANALYSIS_DIR = os.path.join(REPO_ROOT, "tests", "analysis_json")

FAKE_TOOLS = Tools("/opt/ffmpeg/bin/ffmpeg", "/opt/ffmpeg/bin/ffprobe")
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
        self.assertEqual(build_sidecar_argvs(FAKE_TOOLS, ["/library/clips/source.mkv"], OUTPUT_PATH, analysis), [])

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


# --- names and sources on a temporary tree ---------------------------------------------


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
        self.root = Path(self.temporary.name) / "library"
        self.root.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

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
