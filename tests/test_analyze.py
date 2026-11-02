"""Tests for vidfunnel.analyze: the decide_* rules from captured probe JSON, the ffmpeg passes on fixtures.

The pure tests read tests/probe_json (made by `python3 -m tests.test_probe
--capture`) and hand-built measurements; they need no ffmpeg. The
integration tests run idet, cropdetect, the packet scans and analyze() on
tests/fixtures and skip with a message when ffmpeg or the fixtures are
missing.

Capture analyze() output for every fixture into tests/analysis_json (plan's
tests read it), from the repo root:

    python3 -m tests.test_analyze --capture
"""

VERSION = "20261102"

import io
import json
import os
import sys
import types
import unittest

from vidfunnel.analyze import (
    aac_bitrate,
    aggregate_crop,
    aggregate_idet,
    analyze,
    classify_idet_window,
    cropdetect_run,
    decide_aspect,
    decide_audio,
    decide_cadence,
    decide_colour,
    decide_decode,
    decide_keyframes,
    decide_pixelart,
    decide_subtitles,
    decide_sync,
    durations_from_packets,
    idet_run,
    measure_durations,
    needs_cropdetect,
    parse_cropdetect,
    parse_idet,
    predicted_score,
    remux_eligible,
    sample_windows,
)
from vidfunnel.ffmpeg import DecodeResult, ToolNotFound, find_tools

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(REPO_ROOT, "tests", "fixtures")
PROBE_DIR = os.path.join(REPO_ROOT, "tests", "probe_json")
CAPTURE_DIR = os.path.join(REPO_ROOT, "tests", "analysis_json")
MEDIA_SKIP_NAMES = ("README.md",)


def load_probe(relative):
    with open(os.path.join(PROBE_DIR, relative + ".json"), encoding="utf-8") as handle:
        return json.load(handle)


def quiet_progress():
    """A progress argument that sends say() lines and live progress into a string buffer."""
    return types.SimpleNamespace(stream=io.StringIO(), quiet=True)


def options(**values):
    defaults = dict(samples=12, full_analysis=False, assume_dar=None, audio_offset=None, audio_strict_aac=False,
                    max_level="4.2", reencode_video=False)
    defaults.update(values)
    return types.SimpleNamespace(**defaults)


def idet_counters(tff=0, bff=0, progressive=0, undetermined=0, top=0, bottom=0, neither=None):
    frames = tff + bff + progressive + undetermined
    return {"tff": tff, "bff": bff, "progressive": progressive, "undetermined": undetermined,
            "single_tff": tff, "single_bff": bff, "single_progressive": progressive,
            "single_undetermined": undetermined,
            "repeated_neither": frames - top - bottom if neither is None else neither,
            "repeated_top": top, "repeated_bottom": bottom}


def video_stream(**fields):
    stream = {"index": 0, "type": "video", "codec": "mpeg2video", "profile": "Main", "level": 8, "level_text": None,
              "pix_fmt": "yuv420p", "width": 720, "height": 480, "sar": "8:9", "dar": "4:3", "fps": 30000 / 1001.0,
              "fps_fraction": "30000/1001", "field_order": None, "color_space": None, "color_primaries": None,
              "color_transfer": None, "bits_per_raw_sample": None}
    stream.update(fields)
    return stream


PROBES_AVAILABLE = os.path.isdir(PROBE_DIR) and bool(os.listdir(PROBE_DIR))
PROBE_SKIP = "no captured probe JSON in '{}' (run python3 -m tests.test_probe --capture)".format(PROBE_DIR)


# --- sampling -------------------------------------------------------------------------------


class SampleWindowTests(unittest.TestCase):
    def test_hundred_seconds(self):
        windows = sample_windows(100.0)
        self.assertEqual(len(windows), 12)
        self.assertEqual(windows[0], (3.0, 3.0))
        self.assertAlmostEqual(windows[-1][0] + windows[-1][1], 97.0, places=3)
        starts = [start for start, _ in windows]
        steps = [later - earlier for earlier, later in zip(starts, starts[1:])]
        self.assertTrue(max(steps) - min(steps) < 0.002, steps)

    def test_short_file_is_one_full_window(self):
        self.assertEqual(sample_windows(30.0), [(0.0, None)])

    def test_unknown_duration_is_one_full_window(self):
        self.assertEqual(sample_windows(None), [(0.0, None)])

    def test_full_analysis_flag(self):
        self.assertEqual(sample_windows(3600.0, full=True), [(0.0, None)])

    def test_sample_count_option(self):
        self.assertEqual(len(sample_windows(600.0, samples=5)), 5)


# --- decode check ---------------------------------------------------------------------------


class DecodeTests(unittest.TestCase):
    def test_clean(self):
        decision = decide_decode(DecodeResult(0, {}, ""))
        self.assertEqual(decision["penalty"], 0)
        self.assertEqual(decision["flags"], [])

    def test_ten_points_per_class(self):
        classes = {"[h264] error while decoding MB # #": 7, "[flv] Error at MB: #": 1}
        decision = decide_decode(DecodeResult(0, classes, ""))
        self.assertEqual(decision["penalty"], 20)
        self.assertEqual(decision["flags"], ["decode-warnings"])
        self.assertEqual(decision["result"]["classes"], classes)
        self.assertEqual(len(decision["penalties"]), 2)

    def test_failed_status(self):
        decision = decide_decode(DecodeResult(1, {"[flv] Error at MB: #": 1}, ""))
        self.assertEqual(decision["flags"], ["decode-warnings", "decode-failed"])

    def test_benign_classes_recorded_not_counted(self):
        classes = {"[aist##:#/pcm_s16le] Guessed Channel Layout: stereo": 1,
                   "[mpeg] start time for stream # is not set in estimate_timings_from_pts": 1}
        decision = decide_decode(DecodeResult(0, classes, ""))
        self.assertEqual(decision["penalty"], 0)
        self.assertEqual(decision["flags"], [])
        self.assertEqual(decision["result"]["ignored"], classes)


# --- cropdetect and aspect ------------------------------------------------------------------


class CropTests(unittest.TestCase):
    def test_parse_last_crop(self):
        stderr = ("[Parsed_cropdetect_0 @ 0x1] x1:0 x2:719 y1:0 y2:479 w:720 h:480 x:0 y:0 crop=720:480:0:0\n"
                  "[Parsed_cropdetect_0 @ 0x1] x1:0 x2:719 y1:60 y2:419 w:720 h:360 x:0 y:60 crop=720:360:0:60\n")
        self.assertEqual(parse_cropdetect(stderr), {"w": 720, "h": 360, "x": 0, "y": 60})
        self.assertIsNone(parse_cropdetect("no crop lines here"))

    def test_agreement_at_three_quarters(self):
        letterbox = {"w": 720, "h": 360, "x": 0, "y": 60}
        nearly = {"w": 720, "h": 358, "x": 0, "y": 62}
        other = {"w": 720, "h": 480, "x": 0, "y": 0}
        self.assertEqual(aggregate_crop([letterbox, nearly, letterbox, other], 720, 480), letterbox)
        self.assertIsNone(aggregate_crop([letterbox, letterbox, other, other], 720, 480))
        self.assertIsNone(aggregate_crop([letterbox, letterbox, None, None], 720, 480))

    def test_full_frame_is_no_bars(self):
        full = {"w": 720, "h": 480, "x": 0, "y": 0}
        self.assertIsNone(aggregate_crop([full] * 4, 720, 480))

    def test_empty(self):
        self.assertIsNone(aggregate_crop([], 720, 480))


@unittest.skipUnless(PROBES_AVAILABLE, PROBE_SKIP)
class AspectTests(unittest.TestCase):
    letterbox_crop = {"w": 720, "h": 360, "x": 0, "y": 60}

    def test_letterbox_square_pixels_choose_four_by_three(self):
        stream = load_probe("letterbox_720x480_sar_1x1.mkv")["video"]
        self.assertTrue(needs_cropdetect(stream))
        decision = decide_aspect(stream, self.letterbox_crop)
        self.assertEqual(decision["result"]["sar"], "8:9")
        self.assertEqual(decision["result"]["dar"], "4:3")
        self.assertEqual(decision["result"]["source"], "bars")
        self.assertEqual(decision["result"]["candidate"], "4:3")
        self.assertEqual(decision["penalty"], 15)
        self.assertEqual(decision["flags"], [])

    def test_letterbox_without_bars_is_ambiguous(self):
        stream = load_probe("letterbox_720x480_sar_1x1.mkv")["video"]
        decision = decide_aspect(stream, None)
        self.assertEqual(decision["result"]["source"], "ambiguous")
        self.assertEqual(decision["result"]["sar"], "1:1")
        self.assertEqual(decision["flags"], ["aspect-ambiguous"])
        self.assertEqual(decision["penalty"], 30)

    def test_assume_dar_resolves(self):
        stream = load_probe("letterbox_720x480_sar_1x1.mkv")["video"]
        decision = decide_aspect(stream, None, assume_dar="16:9")
        self.assertEqual(decision["result"]["sar"], "32:27")
        self.assertEqual(decision["result"]["dar"], "16:9")
        self.assertEqual(decision["result"]["source"], "assumed")
        self.assertEqual(decision["penalty"], 0)

    def test_bars_on_no_standard_ratio_fall_back_to_ambiguous(self):
        stream = load_probe("letterbox_720x480_sar_1x1.mkv")["video"]
        decision = decide_aspect(stream, {"w": 720, "h": 300, "x": 0, "y": 90})
        self.assertEqual(decision["result"]["source"], "ambiguous")
        self.assertIn("no standard ratio", decision["reason"])

    def test_ntsc_sixteen_by_nine_keeps_signalled(self):
        stream = load_probe("mpeg2_ps_ntsc_dar_16x9_ac3.mpg")["video"]
        self.assertFalse(needs_cropdetect(stream))
        decision = decide_aspect(stream, None)
        self.assertEqual(decision["result"]["sar"], "32:27")
        self.assertEqual(decision["result"]["dar"], "16:9")
        self.assertEqual(decision["result"]["source"], "signalled")
        self.assertEqual(decision["penalty"], 0)

    def test_pal_keeps_signalled(self):
        decision = decide_aspect(load_probe("mpeg2_ps_pal_576_25fps_ac3.mpg")["video"], None)
        self.assertEqual(decision["result"]["sar"], "16:15")
        self.assertEqual(decision["result"]["source"], "signalled")
        self.assertEqual(decision["penalty"], 0)

    def test_small_square_frame(self):
        stream = load_probe("xvid_avi_mp3.avi")["video"]
        self.assertFalse(needs_cropdetect(stream))
        decision = decide_aspect(stream, None)
        self.assertEqual(decision["result"]["source"], "square")
        self.assertEqual(decision["result"]["sar"], "1:1")
        self.assertEqual(decision["penalty"], 0)
        self.assertEqual(decision["flags"], [])

    def test_pal_square_pixels_with_bars(self):
        stream = video_stream(width=720, height=576, sar="1:1", dar="5:4", fps=25.0, fps_fraction="25/1")
        decision = decide_aspect(stream, {"w": 720, "h": 432, "x": 0, "y": 72})
        self.assertEqual(decision["result"]["sar"], "16:15")
        self.assertEqual(decision["result"]["source"], "bars")


# --- idet and cadence -----------------------------------------------------------------------


IDET_STDERR = """
[Parsed_idet_0 @ 0x55c3] Repeated Fields: Neither:     0 Top:     0 Bottom:     0
[Parsed_idet_0 @ 0x55c3] Single frame detection: TFF:     0 BFF:     0 Progressive:     0 Undetermined:     0
[Parsed_idet_0 @ 0x55c3] Multi frame detection: TFF:     0 BFF:     0 Progressive:     0 Undetermined:     0
[Parsed_idet_0 @ 0x7fc5] Repeated Fields: Neither:    94 Top:    28 Bottom:    28
[Parsed_idet_0 @ 0x7fc5] Single frame detection: TFF:   120 BFF:     0 Progressive:    13 Undetermined:    17
[Parsed_idet_0 @ 0x7fc5] Multi frame detection: TFF:   149 BFF:     0 Progressive:     0 Undetermined:     1
"""


class IdetTests(unittest.TestCase):
    def test_parse_uses_last_summary(self):
        counters = parse_idet(IDET_STDERR)
        self.assertEqual(counters["tff"], 149)
        self.assertEqual(counters["undetermined"], 1)
        self.assertEqual(counters["single_progressive"], 13)
        self.assertEqual((counters["repeated_top"], counters["repeated_bottom"]), (28, 28))
        self.assertIsNone(parse_idet("nothing"))

    def test_window_classes(self):
        self.assertEqual(classify_idet_window(idet_counters(tff=150)), ("interlaced", "tff"))
        self.assertEqual(classify_idet_window(idet_counters(bff=100, progressive=20)), ("interlaced", "bff"))
        self.assertEqual(classify_idet_window(idet_counters(tff=149, undetermined=1, top=28, bottom=28)),
                         ("telecined", "tff"))
        self.assertEqual(classify_idet_window(idet_counters(progressive=76, undetermined=49)), ("progressive", None))
        self.assertEqual(classify_idet_window(idet_counters(undetermined=75)), ("progressive", None))
        self.assertEqual(classify_idet_window(None), ("progressive", None))

    def test_telecine_pattern_in_a_mostly_progressive_window(self):
        counters = idet_counters(tff=40, progressive=60, top=15, bottom=15)
        self.assertEqual(classify_idet_window(counters), ("telecined", "tff"))

    def test_repeats_without_combing_are_progressive(self):
        self.assertEqual(classify_idet_window(idet_counters(progressive=100, top=20, bottom=20)),
                         ("progressive", None))

    def test_aggregate_kinds(self):
        interlaced = idet_counters(tff=150)
        progressive = idet_counters(progressive=150)
        telecined = idet_counters(tff=149, undetermined=1, top=28, bottom=28)
        bottom_first = idet_counters(bff=150)
        self.assertEqual(aggregate_idet([progressive] * 3)["kind"], "progressive")
        summary = aggregate_idet([interlaced] * 3)
        self.assertEqual((summary["kind"], summary["order"]), ("interlaced", "tff"))
        self.assertEqual(aggregate_idet([telecined, progressive, telecined], 30000 / 1001.0)["kind"], "telecined")
        self.assertEqual(aggregate_idet([telecined] * 2, 25.0)["kind"], "mixed")
        self.assertEqual(aggregate_idet([interlaced, progressive])["kind"], "mixed")
        self.assertEqual(aggregate_idet([interlaced, bottom_first])["kind"], "mixed")
        self.assertEqual(aggregate_idet([interlaced, telecined], 30000 / 1001.0)["kind"], "mixed")

    def test_all_undetermined_is_progressive_without_evidence(self):
        summary = aggregate_idet([idet_counters(undetermined=75)])
        self.assertEqual(summary["kind"], "progressive")
        self.assertEqual(summary["determined_frames"], 0)


class CadenceTests(unittest.TestCase):
    def summary(self, *windows):
        return aggregate_idet(list(windows), 30000 / 1001.0)

    def test_progressive(self):
        decision = decide_cadence(self.summary(idet_counters(progressive=150)), video_stream())
        self.assertEqual(decision["result"]["kind"], "progressive")
        self.assertIsNone(decision["result"]["filter"])
        self.assertEqual(decision["result"]["output_fps"], "30000/1001")
        self.assertEqual(decision["penalty"], 0)

    def test_telecined(self):
        decision = decide_cadence(self.summary(idet_counters(tff=149, undetermined=1, top=28, bottom=28)),
                                  video_stream(field_order="tt"))
        self.assertEqual(decision["result"]["filter"], "fieldmatch,decimate")
        self.assertEqual(decision["result"]["output_fps"], "24000/1001")
        self.assertEqual(decision["penalty"], 10)

    def test_interlaced_sd_field_rate(self):
        decision = decide_cadence(self.summary(idet_counters(tff=150)), video_stream(field_order="tt"))
        self.assertEqual(decision["result"]["filter"], "bwdif=mode=1:parity=0")
        self.assertEqual(decision["result"]["output_fps"], "60000/1001")
        self.assertEqual(decision["result"]["fps_multiplier"], 2.0)
        self.assertEqual(decision["result"]["level"], "4.1")
        self.assertEqual(decision["penalty"], 5)
        self.assertEqual(decision["flags"], [])

    def test_interlaced_bottom_first(self):
        decision = decide_cadence(self.summary(idet_counters(bff=150)), video_stream())
        self.assertEqual(decision["result"]["filter"], "bwdif=mode=1:parity=1")

    def test_mixed(self):
        decision = decide_cadence(self.summary(idet_counters(tff=150), idet_counters(progressive=150)),
                                  video_stream())
        self.assertEqual(decision["result"]["kind"], "mixed")
        self.assertEqual(decision["result"]["filter"], "bwdif=mode=1:parity=0")
        self.assertEqual(decision["flags"], ["cadence-mixed"])
        self.assertEqual(decision["penalty"], 40)

    def test_interlaced_hd_level_four_two(self):
        stream = video_stream(codec="h264", width=1920, height=1080, sar="1:1", field_order="tt")
        decision = decide_cadence(self.summary(idet_counters(tff=120)), stream)
        self.assertEqual(decision["result"]["level"], "4.2")
        self.assertEqual(decision["result"]["output_fps"], "60000/1001")
        self.assertEqual(decision["flags"], ["level-4.2"])

    def test_interlaced_hd_max_level_four_one(self):
        stream = video_stream(codec="h264", width=1920, height=1080, sar="1:1", field_order="tt")
        decision = decide_cadence(self.summary(idet_counters(tff=120)), stream, max_level="4.1")
        self.assertEqual(decision["result"]["filter"], "bwdif=mode=0:parity=0")
        self.assertEqual(decision["result"]["output_fps"], "30000/1001")
        self.assertEqual(decision["result"]["fps_multiplier"], 1.0)
        self.assertEqual(decision["result"]["level"], "4.1")
        self.assertEqual(decision["flags"], [])

    def test_all_undetermined_is_progressive(self):
        stream = video_stream(codec="msrle", pix_fmt="pal8", width=160, height=120, fps=15.0, fps_fraction="15/1")
        decision = decide_cadence(self.summary(idet_counters(undetermined=75)), stream)
        self.assertEqual(decision["result"]["kind"], "progressive")
        self.assertEqual(decision["penalty"], 0)

    def test_container_trusted_without_idet_evidence(self):
        decision = decide_cadence(self.summary(idet_counters(undetermined=75)), video_stream(field_order="bb"))
        self.assertEqual(decision["result"]["kind"], "interlaced")
        self.assertEqual(decision["result"]["order"], "bff")

    def test_idet_evidence_beats_container(self):
        decision = decide_cadence(self.summary(idet_counters(progressive=150)), video_stream(field_order="tt"))
        self.assertEqual(decision["result"]["kind"], "progressive")


# --- sync -----------------------------------------------------------------------------------


def timing(start, duration, packet_duration=0.0, index=None, gaps=0):
    entry = {"start": start, "end": start + duration, "duration": duration, "packets": 100,
             "packet_duration": packet_duration, "gaps": gaps, "gap_seconds": 0.1 * gaps}
    if index is not None:
        entry["index"] = index
    return entry


def sync_probe(*indexes):
    return {"audio": [{"index": index, "codec": "aac", "profile": "LC"} for index in indexes]}


class SyncTests(unittest.TestCase):
    def decide(self, audio_start, audio_length, video_length=1000.0, **keywords):
        durations = {"video": timing(0.0, video_length, index=0), "audio": [timing(audio_start, audio_length, index=1)]}
        return decide_sync(sync_probe(1), durations, **keywords)

    def test_drift_corrected(self):
        decision = self.decide(0.0, 1012.5)
        entry = decision["result"][0]
        self.assertAlmostEqual(entry["ratio"], 1.0125)
        self.assertAlmostEqual(entry["atempo"], 1.0125)
        self.assertTrue(entry["needs_reencode"])
        self.assertEqual(decision["flags"], ["sync-drift"])
        self.assertEqual(decision["penalty"], 20)

    def test_drift_within_tolerance(self):
        decision = self.decide(0.0, 1000.5)
        entry = decision["result"][0]
        self.assertAlmostEqual(entry["ratio"], 1.0005)
        self.assertIsNone(entry["atempo"])
        self.assertFalse(entry["needs_reencode"])
        self.assertEqual(decision["flags"], [])
        self.assertEqual(decision["penalty"], 0)

    def test_drift_large_is_hold(self):
        decision = self.decide(0.0, 1030.0)
        entry = decision["result"][0]
        self.assertIsNone(entry["atempo"])
        self.assertFalse(entry["needs_reencode"])
        self.assertEqual(decision["flags"], ["sync-drift-large"])
        self.assertEqual(decision["penalty"], 100)
        self.assertEqual(predicted_score(decision["penalties"]), 0)

    def test_offset_flagged_and_kept(self):
        decision = self.decide(0.279, 1000.0)
        entry = decision["result"][0]
        self.assertAlmostEqual(entry["offset"], 0.279)
        self.assertEqual(entry["offset_source"], "measured")
        self.assertEqual(decision["flags"], ["sync-offset"])
        self.assertEqual(decision["penalty"], 0)

    def test_aac_priming_not_flagged(self):
        decision = self.decide(-0.021, 1000.0)
        self.assertAlmostEqual(decision["result"][0]["offset"], -0.021)
        self.assertEqual(decision["flags"], [])

    def test_audio_offset_option_overrides(self):
        decision = self.decide(0.279, 1000.0, audio_offset=0.05)
        entry = decision["result"][0]
        self.assertEqual(entry["offset"], 0.05)
        self.assertAlmostEqual(entry["measured_offset"], 0.279)
        self.assertEqual(entry["offset_source"], "option")
        self.assertEqual(decision["flags"], [])

    def test_packet_quantisation_is_not_drift(self):
        durations = {"video": timing(0.0, 5.0, 0.04, index=0), "audio": [timing(0.0, 5.041, 0.026, index=1)]}
        entry = decide_sync(sync_probe(1), durations)["result"][0]
        self.assertTrue(entry["quantised"])
        self.assertEqual(entry["ratio"], 1.0)
        self.assertIsNone(entry["atempo"])

    def test_gaps_need_reencode(self):
        durations = {"video": timing(0.0, 1000.0, index=0), "audio": [timing(0.0, 1000.0, index=1, gaps=2)]}
        decision = decide_sync(sync_probe(1), durations)
        self.assertTrue(decision["result"][0]["needs_reencode"])
        self.assertEqual(decision["flags"], ["sync-gaps"])

    def test_unmeasured(self):
        decision = decide_sync(sync_probe(1), {"video": None, "audio": []})
        self.assertIsNone(decision["result"][0]["ratio"])
        self.assertEqual(decision["flags"], ["sync-unmeasured"])

    def test_no_audio(self):
        decision = decide_sync({"audio": []}, {"video": timing(0.0, 5.0, index=0), "audio": []})
        self.assertEqual(decision["result"], [])


class PacketTimingTests(unittest.TestCase):
    def test_matroska_priming_from_initial_padding(self):
        packets = [{"pts_time": "-0.021000", "duration_time": "0.021000"}]
        packets += [{"pts_time": "{:.6f}".format(0.021 * step), "duration_time": "0.021000"} for step in range(10)]
        result = durations_from_packets(packets, 48000, 1024)
        self.assertAlmostEqual(result["start"], 0.000333, places=6)
        self.assertAlmostEqual(result["end"], 0.21, places=6)
        self.assertEqual(result["gaps"], 0)

    def test_mp4_priming_from_skip_samples(self):
        packets = [{"pts_time": "-0.021333", "duration_time": "0.021333",
                    "side_data_list": [{"skip_samples": 1024, "discard_padding": 0}]},
                   {"pts_time": "0.000000", "duration_time": "0.021333"}]
        result = durations_from_packets(packets, 48000, 0)
        self.assertAlmostEqual(result["start"], 0.0, places=6)

    def test_missing_pts_uses_dts_and_missing_duration_uses_median(self):
        packets = [{"pts_time": "N/A", "dts_time": "0.000"}, {"dts_time": "0.040", "duration_time": "0.040"},
                   {"pts_time": "0.080"}]
        result = durations_from_packets(packets)
        self.assertEqual(result["start"], 0.0)
        self.assertAlmostEqual(result["end"], 0.12)

    def test_gap_counted(self):
        packets = [{"pts_time": "0.000", "duration_time": "0.020"}, {"pts_time": "0.020", "duration_time": "0.020"},
                   {"pts_time": "0.540", "duration_time": "0.020"}]
        result = durations_from_packets(packets)
        self.assertEqual(result["gaps"], 1)
        self.assertAlmostEqual(result["largest_gap_s"], 0.5)

    def test_video_gaps_not_counted(self):
        packets = [{"pts_time": "0.000", "duration_time": "0.020"}, {"pts_time": "0.540", "duration_time": "0.020"}]
        self.assertIsNone(durations_from_packets(packets, count_gaps=False)["gaps"])

    def test_no_times(self):
        self.assertIsNone(durations_from_packets([{"pts_time": "N/A"}])["duration"])


# --- colour, pixel art, audio, subtitles ----------------------------------------------------


@unittest.skipUnless(PROBES_AVAILABLE, PROBE_SKIP)
class ColourTests(unittest.TestCase):
    def test_ntsc_480(self):
        decision = decide_colour(load_probe("mpeg2_ps_ntsc_dar_4x3_ac3.mpg")["video"])
        self.assertEqual(decision["result"], {"range": "tv", "space": "smpte170m", "primaries": "smpte170m",
                                              "trc": "smpte170m", "source": "inferred"})

    def test_pal_576(self):
        result = decide_colour(load_probe("mpeg2_ps_pal_576_25fps_ac3.mpg")["video"])["result"]
        self.assertEqual((result["space"], result["primaries"], result["trc"]), ("bt470bg", "bt470bg", "bt709"))

    def test_pal_288_at_25(self):
        result = decide_colour(video_stream(width=352, height=288, fps=25.0, fps_fraction="25/1"))["result"]
        self.assertEqual(result["space"], "bt470bg")

    def test_small_25_fps_is_pal_family(self):
        result = decide_colour(load_probe("h264_high_mkv_aac.mkv")["video"])["result"]
        self.assertEqual(result["space"], "bt470bg")

    def test_720p(self):
        result = decide_colour(video_stream(codec="h264", width=1280, height=720, fps=50.0))["result"]
        self.assertEqual((result["space"], result["primaries"], result["trc"]), ("bt709", "bt709", "bt709"))

    def test_tagged_kept(self):
        stream = video_stream(color_space="bt709", color_primaries="bt709", color_transfer="bt709")
        result = decide_colour(stream)["result"]
        self.assertEqual(result["source"], "signalled")
        self.assertEqual(result["space"], "bt709")
        self.assertEqual(result["range"], "tv")


@unittest.skipUnless(PROBES_AVAILABLE, PROBE_SKIP)
class PixelArtTests(unittest.TestCase):
    def test_msrle_160_triples(self):
        decision = decide_pixelart(load_probe("palette_msrle_avi_160x120.avi")["video"])
        self.assertEqual(decision["result"], {"filter": "scale=iw*3:ih*3:flags=neighbor", "factor": 3,
                                              "tune": "animation"})
        self.assertEqual(decision["flags"], ["pixelart-upscale"])

    def test_msrle_320_doubles(self):
        decision = decide_pixelart(load_probe("palette_msrle_avi_320x240.avi")["video"])
        self.assertEqual(decision["result"]["factor"], 2)
        self.assertEqual(decision["result"]["filter"], "scale=iw*2:ih*2:flags=neighbor")

    def test_not_palette(self):
        decision = decide_pixelart(load_probe("xvid_avi_mp3.avi")["video"])
        self.assertIsNone(decision["result"]["filter"])
        self.assertEqual(decision["flags"], [])

    def test_palette_too_tall(self):
        decision = decide_pixelart(video_stream(codec="qtrle", pix_fmt="rgb24", width=640, height=480))
        self.assertIsNone(decision["result"]["filter"])


@unittest.skipUnless(PROBES_AVAILABLE, PROBE_SKIP)
class AudioTests(unittest.TestCase):
    no_sync = {"index": 1, "atempo": None, "needs_reencode": False, "gaps": 0}

    def test_aac_copied(self):
        stream = load_probe("h264_high_mkv_aac.mkv")["audio"][0]
        decision = decide_audio(stream, self.no_sync)
        self.assertEqual(decision["result"]["action"], "copy")
        self.assertIsNone(decision["result"]["filters"])
        self.assertIsNone(decision["result"]["bitrate"])

    def test_mp3_copied(self):
        decision = decide_audio(load_probe("xvid_avi_mp3.avi")["audio"][0], self.no_sync)
        self.assertEqual(decision["result"]["action"], "copy")

    def test_mp3_strict(self):
        decision = decide_audio(load_probe("xvid_avi_mp3.avi")["audio"][0], self.no_sync, strict_aac=True)
        self.assertEqual(decision["result"]["action"], "aac")
        self.assertEqual(decision["result"]["bitrate"], "192k")
        self.assertEqual(decision["result"]["filters"], "aresample=async=1")
        self.assertEqual(decision["flags"], [])

    def test_aac_with_atempo_reencoded(self):
        stream = load_probe("h264_mkv_aac_drift_long.mkv")["audio"][0]
        sync = {"index": 1, "atempo": 1.0125, "needs_reencode": True, "gaps": 0}
        decision = decide_audio(stream, sync)
        self.assertEqual(decision["result"]["action"], "aac")
        self.assertEqual(decision["result"]["filters"], "aresample=async=1,atempo=1.0125")
        self.assertEqual(decision["flags"], ["audio-reencoded-for-sync"])
        self.assertIn("sync", decision["reason"])

    def test_ac3_five_one(self):
        decision = decide_audio(load_probe("h264_mkv_ac3_5point1.mkv")["audio"][0], self.no_sync)
        self.assertEqual(decision["result"]["action"], "aac")
        self.assertEqual(decision["result"]["bitrate"], "448k")
        self.assertEqual(decision["result"]["channels"], 6)

    def test_pcm_mono(self):
        stream = load_probe("palette_msrle_avi_160x120.avi")["audio"][0]
        decision = decide_audio(stream, self.no_sync)
        self.assertEqual(decision["result"]["bitrate"], "96k")
        self.assertEqual(decision["result"]["sample_rate"], 22050)

    def test_bitrate_table(self):
        self.assertEqual([aac_bitrate(count) for count in (1, 2, 6, 8, None)], ["96k", "192k", "448k", "448k", "192k"])


@unittest.skipUnless(PROBES_AVAILABLE, PROBE_SKIP)
class SubtitleTests(unittest.TestCase):
    def test_subrip(self):
        decision = decide_subtitles(load_probe("h264_mkv_srt_subtitles.mkv")["subtitles"])
        entry = decision["result"][0]
        self.assertEqual((entry["codec"], entry["action"], entry["sidecar"]), ("subrip", "mov_text", None))
        self.assertEqual(entry["language"], "eng")
        self.assertEqual(decision["flags"], [])

    def test_ass_gets_srt_sidecar(self):
        entry = decide_subtitles(load_probe("h264_mkv_ass_subtitles.mkv")["subtitles"])["result"][0]
        self.assertEqual((entry["action"], entry["sidecar"]), ("mov_text", ".srt"))

    def test_bitmap_sidecars(self):
        streams = [{"index": 3, "codec": "dvd_subtitle", "language": "fre"},
                   {"index": 4, "codec": "hdmv_pgs_subtitle", "language": None},
                   {"index": 5, "codec": "dvb_teletext", "language": None}]
        decision = decide_subtitles(streams)
        self.assertEqual([(entry["action"], entry["sidecar"]) for entry in decision["result"]],
                         [("sidecar", ".subs.mkv"), ("sidecar", ".sup"), ("unsupported", None)])
        self.assertEqual(decision["flags"], ["subs-sidecar", "subs-unsupported"])

    def test_none(self):
        self.assertEqual(decide_subtitles([])["result"], [])


# --- remux eligibility, keyframes, score ----------------------------------------------------


def cadence_of(kind):
    return {"result": {"kind": kind}}


@unittest.skipUnless(PROBES_AVAILABLE, PROBE_SKIP)
class RemuxTests(unittest.TestCase):
    def test_h264_high_mkv(self):
        eligible, reason = remux_eligible(load_probe("h264_high_mkv_aac.mkv")["video"], cadence_of("progressive"),
                                          options())
        self.assertTrue(eligible, reason)

    def test_reencode_video_option(self):
        eligible, _ = remux_eligible(load_probe("h264_high_mkv_aac.mkv")["video"], cadence_of("progressive"),
                                     options(reencode_video=True))
        self.assertFalse(eligible)

    def test_xvid(self):
        eligible, reason = remux_eligible(load_probe("xvid_avi_mp3.avi")["video"], cadence_of("progressive"), options())
        self.assertFalse(eligible)
        self.assertIn("mpeg4", reason)

    def test_interlaced_ts(self):
        stream = load_probe("h264_1080i_tff_ts_ac3.ts")["video"]
        summary = aggregate_idet([idet_counters(tff=120)], stream["fps"])
        cadence = decide_cadence(summary, stream)
        eligible, reason = remux_eligible(stream, cadence, options())
        self.assertFalse(eligible)
        self.assertIn("interlaced", reason)

    def test_long_gop_flag(self):
        self.assertEqual(decide_keyframes({"keyframes": 2, "max_gap_s": 8.342})["flags"], ["long-gop"])
        self.assertEqual(decide_keyframes({"keyframes": 3, "max_gap_s": 2.0})["flags"], [])


class ScoreTests(unittest.TestCase):
    def test_arithmetic(self):
        self.assertEqual(predicted_score([]), 100)
        self.assertEqual(predicted_score([{"code": "a", "points": 15}, {"code": "b", "points": 5}]), 80)
        self.assertEqual(predicted_score([{"code": "sync-drift-large", "points": 100},
                                          {"code": "aspect-ambiguous", "points": 30}]), 0)


# --- integration: ffmpeg on fixtures --------------------------------------------------------


def fixture_files():
    found = []
    if not os.path.isdir(FIXTURE_DIR):
        return found
    for folder, _, names in os.walk(FIXTURE_DIR):
        for name in names:
            if name not in MEDIA_SKIP_NAMES:
                found.append(os.path.relpath(os.path.join(folder, name), FIXTURE_DIR))
    return sorted(found)


def _tools_or_none():
    try:
        return find_tools()
    except ToolNotFound:
        return None


TOOLS = _tools_or_none()
FIXTURES = fixture_files()
SKIP_REASON = None
if TOOLS is None:
    SKIP_REASON = "ffmpeg/ffprobe not found on PATH; analyze integration tests skipped"
elif not FIXTURES:
    SKIP_REASON = "no fixtures in '{}' (run tests/make_fixtures.sh); analyze integration tests skipped".format(
        FIXTURE_DIR)


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class FixtureTests(unittest.TestCase):
    def fixture(self, relative):
        path = os.path.join(FIXTURE_DIR, relative)
        if not os.path.exists(path):
            self.skipTest("fixture '{}' was not made".format(relative))
        return path

    def cadence_of_fixture(self, relative):
        from vidfunnel.probe import probe_file
        path = self.fixture(relative)
        probe = probe_file(TOOLS, path)
        windows = sample_windows(probe["format"]["duration"])
        per_window = idet_run(TOOLS, path, windows, quiet_progress())
        return aggregate_idet(per_window, probe["video"]["fps"])

    def test_idet_interlaced(self):
        summary = self.cadence_of_fixture("mpeg2_ps_interlaced_tff_ac3.mpg")
        self.assertEqual((summary["kind"], summary["order"]), ("interlaced", "tff"))

    def test_idet_telecined(self):
        self.assertEqual(self.cadence_of_fixture("mpeg2_ps_telecined_ac3.mpg")["kind"], "telecined")

    def test_idet_progressive(self):
        self.assertEqual(self.cadence_of_fixture("h264_high_mkv_aac.mkv")["kind"], "progressive")

    def test_idet_palette_all_undetermined(self):
        summary = self.cadence_of_fixture("palette_msrle_avi_160x120.avi")
        self.assertEqual(summary["kind"], "progressive")
        self.assertEqual(summary["determined_frames"], 0)

    def test_cropdetect_letterbox(self):
        path = self.fixture("letterbox_720x480_sar_1x1.mkv")
        per_window = cropdetect_run(TOOLS, path, [(0.0, None)], quiet_progress())
        self.assertEqual(aggregate_crop(per_window, 720, 480), {"w": 720, "h": 360, "x": 0, "y": 60})

    def test_cropdetect_sampled_windows(self):
        path = self.fixture("letterbox_720x480_sar_1x1.mkv")
        per_window = cropdetect_run(TOOLS, path, [(0.5, 1.0), (2.0, 1.0), (3.5, 1.0)], quiet_progress())
        letterbox = {"w": 720, "h": 360, "x": 0, "y": 60}
        # The test pattern's left 90 columns stay dark through the third second, so the last window alone
        # reads 630x360 at 90,60: two windows of three agree, under 75 %, so no bars are reported.
        self.assertEqual(per_window[:2], [letterbox, letterbox])
        self.assertEqual(per_window[2], {"w": 630, "h": 360, "x": 90, "y": 60})
        self.assertIsNone(aggregate_crop(per_window, 720, 480))
        self.assertEqual(aggregate_crop(per_window[:2], 720, 480), letterbox)

    def test_cropdetect_no_bars(self):
        path = self.fixture("h264_high_mkv_aac.mkv")
        per_window = cropdetect_run(TOOLS, path, [(0.0, None)], quiet_progress())
        self.assertIsNone(aggregate_crop(per_window, 320, 240))

    def test_measure_drift(self):
        from vidfunnel.probe import probe_file
        path = self.fixture("h264_mkv_aac_drift_long.mkv")
        durations = measure_durations(TOOLS, path, probe_file(TOOLS, path), quiet_progress())
        ratio = durations["audio"][0]["duration"] / durations["video"]["duration"]
        # The audio input is 5.1712 s. ffprobe 7 and 8 give the last AAC packet a full
        # 1024-sample frame, so the measured audio is 5.184 s (ratio 1.0125); ffprobe 9
        # reports the last packet's true length, 5.1712 s (ratio 1.0100). Both are drift
        # of about 1 %, which decide_sync corrects with atempo.
        distance = min(abs(ratio - 5.184 / 5.12), abs(ratio - 5.1712 / 5.12))
        self.assertLessEqual(distance, 0.0005, ratio)
        self.assertAlmostEqual(durations["video"]["duration"], 5.12, places=3)

    def test_measure_offset(self):
        from vidfunnel.probe import probe_file
        path = self.fixture("h264_mkv_aac_offset_300ms.mkv")
        probe = probe_file(TOOLS, path)
        durations = measure_durations(TOOLS, path, probe, quiet_progress())
        offset = durations["audio"][0]["start"] - durations["video"]["start"]
        # First audio packet at 0.279 s plus the 1024-sample priming: sound starts at 0.300 s.
        self.assertAlmostEqual(offset, 0.300, delta=0.002)
        self.assertAlmostEqual(durations["audio"][0]["first_pts"], 0.279, places=3)
        self.assertEqual(decide_sync(probe, durations)["flags"], ["sync-offset"])

    def test_measure_within_has_no_drift(self):
        from vidfunnel.probe import probe_file
        path = self.fixture("h264_mkv_aac_drift_within.mkv")
        probe = probe_file(TOOLS, path)
        decision = decide_sync(probe, measure_durations(TOOLS, path, probe, quiet_progress()))
        self.assertEqual(decision["flags"], [])
        self.assertEqual(decision["penalty"], 0)

    def test_analyze_damaged_flv(self):
        from vidfunnel.probe import probe_file
        path = self.fixture("flv1_flv_mp3_damaged.flv")
        result = analyze(TOOLS, path, probe_file(TOOLS, path), options(), quiet_progress())
        self.assertIn("decode-warnings", result["flags"])
        classes = result["decode"]["result"]["classes"]
        self.assertTrue(classes)
        self.assertEqual(result["decode"]["penalty"], 10 * len(classes))
        self.assertTrue(any("corrupt decoded frame" in message for message in classes), classes)
        self.assertEqual(result["predicted_score"], max(0, 100 - sum(entry["points"] for entry in result["penalties"])))
        self.assertLess(result["predicted_score"], 100)
        self.assertIsNone(result["keyframes"])

    def test_analyze_plain_remux_candidate(self):
        from vidfunnel.probe import probe_file
        path = self.fixture("h264_high_mkv_aac.mkv")
        progress = quiet_progress()
        result = analyze(TOOLS, path, probe_file(TOOLS, path), options(), progress)
        self.assertEqual(result["predicted_score"], 100, result["penalties"])
        self.assertEqual(result["flags"], [])
        self.assertTrue(result["remux"]["eligible"])
        self.assertEqual(result["keyframes"]["result"]["keyframes"], 1)
        self.assertEqual(result["windows"], [[0.0, None]])
        self.assertEqual(result["audio"][0]["result"]["action"], "copy")
        self.assertIn("idet over 1 window(s) of 'h264_high_mkv_aac.mkv'", progress.stream.getvalue())

    def test_analyze_interlaced_mpeg2(self):
        from vidfunnel.probe import probe_file
        path = self.fixture("mpeg2_ps_interlaced_tff_ac3.mpg")
        result = analyze(TOOLS, path, probe_file(TOOLS, path), options(), quiet_progress())
        self.assertEqual(result["cadence"]["result"]["filter"], "bwdif=mode=1:parity=0")
        self.assertEqual(result["aspect"]["result"]["source"], "signalled")
        self.assertEqual(result["audio"][0]["result"]["bitrate"], "192k")
        self.assertFalse(result["remux"]["eligible"])
        self.assertEqual(result["predicted_score"], 95)


# --- capture ---------------------------------------------------------------------------------


def capture_all(tools=None):
    """Write tests/analysis_json/<fixture>.json (analyze() output) for every fixture with a video stream."""
    from vidfunnel.probe import probe_file
    tools = tools or find_tools()
    written = []
    for relative in fixture_files():
        path = os.path.join(FIXTURE_DIR, relative)
        probe = probe_file(tools, path)
        if probe["video"] is None:
            print("skipped '{}': no video stream".format(relative))
            continue
        result = analyze(tools, path, probe, options(), quiet_progress())
        target = os.path.join(CAPTURE_DIR, relative + ".json")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
        written.append(target)
    return written


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
