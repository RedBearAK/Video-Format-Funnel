"""Tests for vidfunnel.verify: comparisons, expectations and scoring from hand-built records, and verify runs.

The pure tests need no ffmpeg. The integration tests copy fixtures from
tests/fixtures into a temporary folder, plan them with vidfunnel.plan and
produce each output by running the planned argv themselves (encode.py is
not used here), mark the record 'converted', then verify. The MPEG-2
re-encode runs with '-preset ultrafast' in place of 'slower' so the test
stays quick; the preset changes compression, not frame count, duration or
streams, which is what verify checks. They skip with a message when ffmpeg
or the fixtures are missing.
"""

VERSION = "20261109.2"

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

from vidfunnel.analyze import decide_subtitles
from vidfunnel.ffmpeg import ToolNotFound, find_tools
from vidfunnel.ffmpeg import run as run_tool
from vidfunnel.record import load_record, save_record, set_status
from vidfunnel.verify import (
    PENALTY_IDENTITY,
    add_arguments,
    compare_durations,
    compare_frames,
    compare_streams,
    duration_tolerance,
    expected_frames,
    expected_streams,
    output_path_of,
    output_video_duration,
    record_key,
    run,
    score_record,
    source_video_duration,
    verify_record,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(REPO_ROOT, "tests", "fixtures")
PROBE_DIR = os.path.join(REPO_ROOT, "tests", "probe_json")
ANALYSIS_DIR = os.path.join(REPO_ROOT, "tests", "analysis_json")


def quiet():
    return types.SimpleNamespace(stream=io.StringIO(), quiet=True)


def hand_record(duration=5.0, fps=30.0, nb_frames=None, kind="progressive", multiplier=1.0, output_fps=None,
                penalties=(), status="converted"):
    """A record with only what verify's pure functions read."""
    return {
        "id": "record-under-test",
        "status": status,
        "source": {"name": "clip.avi", "sha256": "0123456789abcdef" + "0" * 48, "kind": "file"},
        "penalties": [dict(entry) for entry in penalties],
        "flags": [],
        "stages": {
            "probe": {"format": {"duration": duration},
                      "video": {"index": 0, "fps": fps, "nb_frames": nb_frames, "duration": duration}},
            "analyze": {"cadence": {"result": {"kind": kind, "fps_multiplier": multiplier,
                                               "output_fps": output_fps}},
                        "penalties": [dict(entry) for entry in penalties]},
        },
    }


def passing_checks():
    return {"decode": {"penalties": []}, "duration": {"ok": True}, "frames": {"ok": True},
            "ssim": {"requested": False, "ok": None}, "identity": {"ok": True}}


# --- pure --------------------------------------------------------------------------------


class CompareTest(unittest.TestCase):

    def test_durations(self):
        within = compare_durations(10.0, 10.05)
        self.assertTrue(within["ok"])
        self.assertAlmostEqual(within["delta"], 0.05)
        outside = compare_durations(10.0, 10.15)
        self.assertFalse(outside["ok"])
        self.assertFalse(compare_durations(None, 10.0)["ok"])
        one_frame = duration_tolerance(25.0)
        self.assertAlmostEqual(one_frame, 0.14)
        self.assertTrue(compare_durations(10.0, 10.14, one_frame)["ok"])
        self.assertFalse(compare_durations(10.0, 10.15, one_frame)["ok"])
        self.assertEqual(duration_tolerance(None), 0.1)

    def test_source_duration_preference(self):
        record = hand_record(duration=5.0)
        record["stages"]["probe"]["format"]["duration"] = 5.2
        self.assertEqual(source_video_duration(record), (5.2, "format"))
        record["stages"]["probe"]["video"]["duration_source"] = "stream"
        self.assertEqual(source_video_duration(record), (5.0, "stream"))
        record["stages"]["analyze"]["sync"] = {"durations": {"video": {"duration": 4.96}}}
        self.assertEqual(source_video_duration(record), (4.96, "packets"))
        self.assertEqual(source_video_duration({"stages": {}}), (None, None))

    def test_output_duration_prefers_video_stream(self):
        probe = {"format": {"duration": 5.2}, "video": {"duration": 5.04}}
        self.assertEqual(output_video_duration(probe), (5.04, "stream"))
        self.assertEqual(output_video_duration({"format": {"duration": 5.2}, "video": {}}), (5.2, "format"))

    def test_frames(self):
        self.assertTrue(compare_frames(150, 150)["ok"])
        self.assertTrue(compare_frames(150, 152)["ok"])
        self.assertTrue(compare_frames(150, 147)["ok"])
        beyond = compare_frames(150, 160)
        self.assertFalse(beyond["ok"])
        self.assertEqual((beyond["low"], beyond["high"]), (145, 155))
        self.assertFalse(compare_frames(None, 150)["ok"])

    def test_streams(self):
        probe = {"streams": [{"type": "video", "disposition": {}}, {"type": "video", "disposition": {"attached_pic": 1}},
                             {"type": "audio"}],
                 "audio": [{"type": "audio"}], "subtitles": []}
        result = compare_streams({"video": 1, "audio": 1, "subtitle": 0}, probe)
        self.assertTrue(result["ok"])
        self.assertFalse(compare_streams({"video": 1, "audio": 2, "subtitle": 0}, probe)["ok"])

    def test_stream_count_mismatch_costs_fifty_points(self):
        from vidfunnel.verify import PENALTY_STREAMS, post_conversion_penalties
        entries = post_conversion_penalties({"streams": {"ok": False, "expected": {"audio": 2}, "actual": {"audio": 1}}})
        self.assertEqual([entry["code"] for entry in entries], ["stream-count"])
        self.assertEqual(entries[0]["points"], PENALTY_STREAMS)
        self.assertEqual(post_conversion_penalties({"streams": {"ok": True}}), [])


class ExpectedFramesTest(unittest.TestCase):

    def test_progressive_from_nb_frames(self):
        self.assertEqual(expected_frames(hand_record(nb_frames=150)), 150)

    def test_field_rate_doubles(self):
        record = hand_record(fps=30000 / 1001, nb_frames=150, kind="interlaced", multiplier=2.0,
                             output_fps="60000/1001")
        self.assertEqual(expected_frames(record), 300)

    def test_measured_count_wins(self):
        self.assertEqual(expected_frames(hand_record(nb_frames=150), measured_source_frames=149), 149)

    def test_telecine_from_duration(self):
        record = hand_record(duration=5.0, fps=30000 / 1001, nb_frames=150, kind="telecined", multiplier=0.8,
                             output_fps="24000/1001")
        self.assertEqual(expected_frames(record), 120)

    def test_variable_rate_without_count_falls_back_to_duration_times_fps(self):
        # No nb_frames, no measured count (the source is gone): duration x fps, documented as an estimate.
        self.assertEqual(expected_frames(hand_record(duration=5.0, fps=30.0, nb_frames=None)), 150)

    def test_unknown(self):
        self.assertIsNone(expected_frames(hand_record(duration=None, fps=None, nb_frames=None)))


class ExpectedStreamsTest(unittest.TestCase):

    def test_text_subtitles_go_into_the_mp4(self):
        with open(os.path.join(PROBE_DIR, "h264_mkv_srt_subtitles.mkv.json"), encoding="utf-8") as handle:
            probe = json.load(handle)
        with open(os.path.join(ANALYSIS_DIR, "h264_mkv_srt_subtitles.mkv.json"), encoding="utf-8") as handle:
            analysis = json.load(handle)
        record = {"stages": {"probe": probe, "analyze": analysis}}
        self.assertEqual(expected_streams(record), {"video": 1, "audio": 1, "subtitle": 1})

    def test_bitmap_subtitles_go_to_a_sidecar(self):
        subtitles = decide_subtitles([{"index": 2, "codec": "dvd_subtitle", "language": "eng"}])
        audio = [{"result": {"index": 1, "action": "aac"}}]
        record = {"stages": {"probe": {}, "analyze": {"audio": audio, "subtitles": subtitles}}}
        self.assertEqual(expected_streams(record), {"video": 1, "audio": 1, "subtitle": 0})


class ScoreTest(unittest.TestCase):

    def test_clean(self):
        self.assertEqual(score_record(hand_record(), passing_checks()), (100, []))

    def test_analysis_and_post_conversion_penalties(self):
        record = hand_record(penalties=[{"code": "interlaced", "points": 5, "reason": "deinterlaced"}])
        checks = passing_checks()
        checks["decode"] = {"penalties": [{"code": "decode-warnings", "points": 10, "reason": "one"},
                                          {"code": "decode-warnings", "points": 10, "reason": "two"}]}
        checks["duration"] = {"ok": False, "source": 5.0, "output": 5.5}
        checks["frames"] = {"ok": False, "expected": 150, "actual": 100, "low": 145, "high": 155}
        checks["ssim"] = {"requested": True, "ok": False, "value": 0.9, "threshold": 0.97}
        score, added = score_record(record, checks)
        self.assertEqual([entry["code"] for entry in added],
                         ["decode-warnings", "decode-warnings", "duration-mismatch", "frame-count", "ssim-low"])
        self.assertEqual(score, 100 - 5 - 20 - 20 - 30 - 25)

    def test_floor_at_zero(self):
        record = hand_record(penalties=[{"code": "cadence-mixed", "points": 40, "reason": "mixed"},
                                        {"code": "aspect-ambiguous", "points": 30, "reason": "ambiguous"}])
        checks = passing_checks()
        checks["frames"] = {"ok": False}
        checks["duration"] = {"ok": False}
        self.assertEqual(score_record(record, checks)[0], 0)

    def test_identity_mismatch_is_zero(self):
        checks = passing_checks()
        checks["identity"] = {"ok": False, "reason": "id differs"}
        score, added = score_record(hand_record(), checks)
        self.assertEqual(score, 0)
        self.assertEqual(added[0]["points"], PENALTY_IDENTITY)

    def test_quarantined_is_zero(self):
        self.assertEqual(score_record(hand_record(status="quarantined"), passing_checks())[0], 0)

    def test_second_run_does_not_double_count(self):
        record = hand_record(penalties=[{"code": "interlaced", "points": 5, "reason": "deinterlaced"}])
        checks = passing_checks()
        checks["duration"] = {"ok": False}
        score, added = score_record(record, checks)
        self.assertEqual(score, 75)
        for entry in added:
            record["penalties"].append(dict(entry, stage="verify"))
        self.assertEqual(score_record(record, checks)[0], 75)
        self.assertEqual(score_record(record, passing_checks())[0], 95)


class ArgumentsTest(unittest.TestCase):

    def test_defaults(self):
        parser = add_arguments(argparse.ArgumentParser())
        args = parser.parse_args(["somewhere"])
        self.assertEqual((args.paths, args.ssim_check, args.ssim_threshold, args.samples),
                         (["somewhere"], False, 0.97, None))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["somewhere", "--ssim-threshold", "1.5"])

    def test_refuses_unconverted_record(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                verify_record(None, Path(folder) / "clip.avi", hand_record(status="planned"), None, quiet())


# --- integration -------------------------------------------------------------------------


def _tools_or_none():
    try:
        return find_tools()
    except ToolNotFound:
        return None


TOOLS = _tools_or_none()
REMUX_FIXTURE = "h264_high_mkv_aac.mkv"
SUBTITLE_FIXTURE = "h264_mkv_srt_subtitles.mkv"
MP4_FIXTURE = "h264_mp4_aac.mp4"
INTERLACED_FIXTURE = "mpeg2_ps_interlaced_tff_ac3.mpg"
XVID_FIXTURE = "xvid_avi_mp3.avi"
INTEGRATION_FIXTURES = (REMUX_FIXTURE, SUBTITLE_FIXTURE, MP4_FIXTURE, INTERLACED_FIXTURE, XVID_FIXTURE)


def _missing_fixtures():
    return [name for name in INTEGRATION_FIXTURES if not os.path.exists(os.path.join(FIXTURE_DIR, name))]


def convert_folder(folder, names, renames=None):
    """Copy fixtures into folder, plan them, run each planned argv, mark converted. {source name: (key, record)}."""
    from vidfunnel.plan import make_options, plan_paths
    from vidfunnel.record import iter_records
    renames = renames or {}
    for name in names:
        shutil.copy2(os.path.join(FIXTURE_DIR, name), str(Path(folder) / renames.get(name, name)))
    plan_paths(TOOLS, [str(folder)], make_options(), progress=quiet())
    converted = {}
    for record_folder, record in iter_records(folder):
        if record.get("status") != "planned":
            raise AssertionError("'{}' planned as '{}'".format(record["source"]["name"], record.get("status")))
        # Ultrafast instead of slower for the re-encodes (MPEG-2, Xvid): speed only, verify's checks do not depend on it.
        argv = ["ultrafast" if part == "slower" else part for part in record["stages"]["plan"]["argv"]]
        run_tool(argv)
        set_status(record, "converted")
        key = record_key(record_folder, record)
        save_record(key, record)
        converted[record["source"]["name"]] = (key, record)
    return converted


@unittest.skipIf(TOOLS is None, "ffmpeg not found on PATH; verify integration tests skipped")
class VerifyRunTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.temporary = None
        if _missing_fixtures():
            return
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name) / "library"
        cls.root.mkdir()
        cls.converted = convert_folder(cls.root, INTEGRATION_FIXTURES)
        cls.verified = {}
        for name, (key, record) in cls.converted.items():
            cls.verified[name] = verify_record(TOOLS, key, record, None, quiet())

    @classmethod
    def tearDownClass(cls):
        if cls.temporary is not None:
            cls.temporary.cleanup()

    def setUp(self):
        missing = _missing_fixtures()
        if missing:
            self.skipTest("fixtures missing ({}); run tests/make_fixtures.sh".format(", ".join(missing)))

    def test_remux_mkv(self):
        record = self.verified[REMUX_FIXTURE]
        stage = record["stages"]["verify"]
        self.assertEqual(record["status"], "verified")
        self.assertEqual(record["score"], 100)
        self.assertTrue(stage["duration"]["ok"])
        self.assertEqual(stage["frames"]["actual"], stage["frames"]["source_frames"])
        self.assertEqual(stage["frames"]["source_frames_from"], "counted")
        self.assertEqual(stage["streams"]["actual"], {"video": 1, "audio": 1, "subtitle": 0})
        self.assertTrue(stage["identity"]["ok"])
        self.assertEqual(stage["identity"]["tag"]["id"], record["id"])
        self.assertEqual(stage["output_probe"]["video"]["codec"], "h264")
        output = output_path_of(self.root, record)
        with open(str(output), "rb") as handle:
            self.assertEqual(record["output"]["sha256"], hashlib.sha256(handle.read()).hexdigest())
        self.assertEqual(record["output"]["size"], output.stat().st_size)
        self.assertEqual(load_record(self.converted[REMUX_FIXTURE][0])["score"], 100)

    def test_subtitles_reach_the_output(self):
        stage = self.verified[SUBTITLE_FIXTURE]["stages"]["verify"]
        self.assertEqual(stage["streams"]["actual"]["subtitle"], 1)
        self.assertTrue(stage["streams"]["ok"])

    def test_mp4_source_uses_interim_name(self):
        record = self.verified[MP4_FIXTURE]
        self.assertEqual(record["output"]["interim_name"], "h264_mp4_aac.vidfunnel.mp4")
        self.assertEqual(record["stages"]["verify"]["output_name"], "h264_mp4_aac.vidfunnel.mp4")
        self.assertEqual(record["status"], "verified")
        self.assertEqual(record["score"], 100)

    def test_interlaced_field_rate(self):
        record = self.verified[INTERLACED_FIXTURE]
        stage = record["stages"]["verify"]
        frames = stage["frames"]
        self.assertEqual(frames["fps_multiplier"], 2.0)
        self.assertTrue(frames["ok"])
        self.assertAlmostEqual(frames["actual"] / float(frames["source_frames"]), 2.0, delta=0.05)
        self.assertAlmostEqual(stage["output_probe"]["video"]["fps"], 60000 / 1001.0, places=2)
        self.assertEqual(stage["penalties"], [])
        self.assertEqual(record["score"], 95)

    def test_avi_reencode_duration(self):
        record = self.verified[XVID_FIXTURE]
        stage = record["stages"]["verify"]
        self.assertEqual(record["action"], "reencode")
        self.assertTrue(stage["duration"]["ok"], stage["duration"])
        self.assertEqual(stage["duration"]["source_kind"], "packets")
        self.assertEqual(stage["duration"]["output_kind"], "stream")
        self.assertNotIn("duration-mismatch", [entry["code"] for entry in stage["penalties"]])
        self.assertEqual(record["score"], 100)

    def test_ssim_check(self):
        key = self.converted[INTERLACED_FIXTURE][0]
        options = types.SimpleNamespace(ssim_check=True, ssim_threshold=0.97, samples=None)
        record = verify_record(TOOLS, key, load_record(key), options, quiet())
        ssim = record["stages"]["verify"]["ssim"]
        self.assertTrue(ssim["requested"])
        self.assertGreaterEqual(ssim["value"], 0.97)
        self.assertTrue(ssim["ok"])
        self.assertEqual(record["score"], 95)

    def test_run_command(self):
        from vidfunnel.cli import EXIT_OK
        args = argparse.Namespace(paths=[str(self.root)], ffmpeg=None, quiet=True, verbose=False, log=None,
                                  yes=False, ssim_check=False, ssim_threshold=0.97, samples=None,
                                  stream=io.StringIO())
        self.assertEqual(run(args), EXIT_OK)
        self.assertIn("5 ok", args.stream.getvalue())

    def test_run_tags_every_line_of_an_item(self):
        from vidfunnel.cli import EXIT_OK
        args = argparse.Namespace(paths=[str(self.root)], ffmpeg=None, quiet=False, verbose=False, log=None,
                                  yes=False, ssim_check=False, ssim_threshold=0.97, samples=None,
                                  stream=io.StringIO())
        self.assertEqual(run(args), EXIT_OK)
        text = args.stream.getvalue()
        tags = set()
        for line in text.splitlines():
            if line.startswith("[verify "):
                tags.add(line.split("]", 1)[0] + "]")
            else:
                self.assertTrue(line.startswith("verify: "), text)
        self.assertEqual(tags, {"[verify {}/5]".format(number) for number in range(1, 6)}, text)
        self.assertIn("] [verify decode] ", text)
        self.assertIn("]   verified '", text)


@unittest.skipIf(TOOLS is None, "ffmpeg not found on PATH; verify integration tests skipped")
class VerifyAgainTest(unittest.TestCase):
    """Damage, a foreign output and a missing output, each on its own converted copy."""

    def setUp(self):
        missing = _missing_fixtures()
        if missing:
            self.skipTest("fixtures missing ({}); run tests/make_fixtures.sh".format(", ".join(missing)))
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "library"
        self.root.mkdir()
        self.key, self.record = convert_folder(self.root, [REMUX_FIXTURE])[REMUX_FIXTURE]
        verify_record(TOOLS, self.key, self.record, None, quiet())
        self.assertEqual(self.record["score"], 100)
        self.output = output_path_of(self.root, self.record)

    def tearDown(self):
        self.temporary.cleanup()

    def test_damaged_output(self):
        size = self.output.stat().st_size
        with open(str(self.output), "r+b") as handle:
            handle.seek(size // 2)
            handle.write(b"\xff" * 64)
        record = verify_record(TOOLS, self.key, load_record(self.key), None, quiet())
        stage = record["stages"]["verify"]
        self.assertEqual(record["status"], "verified")
        self.assertTrue(stage["decode"]["classes"])
        self.assertTrue(stage["penalties"])
        self.assertLess(record["score"], 100)
        self.assertEqual(len(record["stages"]["verify_history"]), 1)
        self.assertEqual(record["penalties"], [dict(entry, stage="verify") for entry in stage["penalties"]])
        self.assertIn("decode-warnings", record["flags"])

    def test_foreign_output(self):
        other = Path(self.temporary.name) / "elsewhere"
        other.mkdir()
        # The subtitle fixture under the remux fixture's name: same output name, another record id and source.
        converted = convert_folder(other, [SUBTITLE_FIXTURE], renames={SUBTITLE_FIXTURE: REMUX_FIXTURE})
        foreign_record = converted[REMUX_FIXTURE][1]
        shutil.copy2(str(output_path_of(other, foreign_record)), str(self.output))
        record = verify_record(TOOLS, self.key, load_record(self.key), None, quiet())
        stage = record["stages"]["verify"]
        self.assertFalse(stage["identity"]["ok"])
        self.assertEqual(stage["identity"]["tag"]["id"], foreign_record["id"])
        self.assertIn("identity-mismatch", [entry["code"] for entry in stage["penalties"]])
        self.assertEqual(record["score"], 0)
        self.assertIn("identity-mismatch", record["flags"])

    def test_missing_output(self):
        self.output.unlink()
        record = verify_record(TOOLS, self.key, load_record(self.key), None, quiet())
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["stages"]["verify"]["error"], "output missing")
        self.assertEqual(load_record(self.key)["status"], "failed")

    def test_output_changed_during_verify(self):
        # The seam: probe_file appends bytes to the output before probing it, standing in for another
        # process writing the file after verify hashed it.
        import vidfunnel.verify as verify_module
        real_probe = verify_module.probe_file

        def probe_after_append(tools, path):
            with open(str(path), "ab") as handle:
                handle.write(b"\x00" * 4096)
            return real_probe(tools, path)

        with unittest.mock.patch.object(verify_module, "probe_file", probe_after_append):
            record = verify_record(TOOLS, self.key, load_record(self.key), None, quiet())
        stage = record["stages"]["verify"]
        self.assertEqual(record["status"], "failed")
        self.assertIs(stage["output_changed_during_verify"], True)
        self.assertIn(self.output.name, stage["error"])
        self.assertNotIn("score", record)
        on_disk = load_record(self.key)
        self.assertEqual(on_disk["status"], "failed")
        self.assertNotIn("score", on_disk)
        self.assertIs(on_disk["stages"]["verify"]["output_changed_during_verify"], True)

    def test_unchanged_output_hash_matches_file(self):
        record = verify_record(TOOLS, self.key, load_record(self.key), None, quiet())
        self.assertEqual(record["status"], "verified")
        self.assertNotIn("output_changed_during_verify", record["stages"]["verify"])
        with open(str(self.output), "rb") as handle:
            self.assertEqual(load_record(self.key)["output"]["sha256"], hashlib.sha256(handle.read()).hexdigest())

    def test_run_does_not_overwrite_a_record_retired_after_collection(self):
        # The seam: FolderLock is wrapped so that, after run() has collected the records and before it
        # takes the folder's lock, the record on disk becomes 'retired' as a retire would leave it.
        import vidfunnel.verify as verify_module
        from vidfunnel.cli import EXIT_OK
        from vidfunnel.record import FolderLock, add_stage
        key = self.key

        class RetireFirst(FolderLock):
            def __enter__(self):
                record = load_record(key)
                add_stage(record, "retire", {"final_name": "retired-elsewhere.mp4"})
                set_status(record, "retired")
                save_record(key, record)
                return super().__enter__()

        args = argparse.Namespace(paths=[str(self.root)], ffmpeg=None, quiet=True, verbose=False, log=None,
                                  yes=False, ssim_check=False, ssim_threshold=0.97, samples=None,
                                  stream=io.StringIO())
        with unittest.mock.patch.object(verify_module, "FolderLock", RetireFirst):
            self.assertEqual(run(args), EXIT_OK)
        on_disk = load_record(key)
        self.assertEqual(on_disk["status"], "retired")
        self.assertEqual(on_disk["stages"]["retire"]["final_name"], "retired-elsewhere.mp4")
        self.assertNotIn("verify_history", on_disk["stages"])
        self.assertEqual(on_disk["score"], 100)
        self.assertIn("now has status 'retired'", args.stream.getvalue())
        self.assertIn("1 skipped", args.stream.getvalue())


    def test_the_counting_lines_carry_the_item_tag_and_go_to_the_item_stream(self):
        """count_frames gets the item's progress: its start line is tagged and goes where the item's lines go."""
        import vidfunnel.probe
        args = argparse.Namespace(paths=[str(self.root)], ffmpeg=None, quiet=False, verbose=False, log=None,
                                  yes=False, ssim_check=False, ssim_threshold=0.97, samples=None,
                                  stream=io.StringIO())
        stdout = io.StringIO()
        with unittest.mock.patch.object(vidfunnel.probe, "ANNOUNCE_BYTES", 0), contextlib.redirect_stdout(stdout):
            run(args)
        counting = [line for line in args.stream.getvalue().splitlines() if "counting packets" in line]
        self.assertEqual(len(counting), 2, "the output's count and the source's count:\n{}".format(
            args.stream.getvalue()))
        for line in counting:
            self.assertTrue(line.startswith("[verify 1/1] counting packets of stream "), line)
        self.assertNotIn("counting packets", stdout.getvalue())


class StopCase(unittest.TestCase):
    """Clean drain flags and stop-file state around each test."""

    def setUp(self):
        import vidfunnel.interrupt
        import vidfunnel.stopfile
        for flag in (vidfunnel.interrupt.interrupted, vidfunnel.interrupt.draining):
            flag.clear()
            self.addCleanup(flag.clear)
        vidfunnel.interrupt.drain_tally.reset()
        self.addCleanup(vidfunnel.interrupt.drain_tally.reset)
        vidfunnel.stopfile.reset()
        self.addCleanup(vidfunnel.stopfile.reset)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "library"
        self.root.mkdir()

    def tally(self):
        import vidfunnel.interrupt
        tally = vidfunnel.interrupt.drain_tally
        return tally.seen, tally.finished, tally.left


class VerifyDrainTests(StopCase):
    """verify's loop asks for a drain before each folder and each record; hand-written records, no ffmpeg."""

    LAYOUT = (("first", "harbour"), ("second", "meadow"), ("second", "orchard"), ("third", "valley"))

    def setUp(self):
        from vidfunnel.record import new_record
        StopCase.setUp(self)
        for folder, stem in self.LAYOUT:
            source = self.root / folder / (stem + ".avi")
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"hand-written source " * 64)
            record = new_record(source)
            set_status(record, "converted")
            record["output"]["name"] = stem + ".mp4"
            save_record(source, record)

    def verify_drained_in(self, draining_stem):
        """verify.run with verify_record replaced; the drain begins in `draining_stem`'s record.

        Returns (stems verified, folders locked)."""
        import vidfunnel.interrupt
        import vidfunnel.verify as verify_module
        verified = []
        locked = []
        real_lock = verify_module.FolderLock

        def lock(folder, *rest, **options):
            locked.append(Path(folder).name)
            return real_lock(folder, *rest, **options)

        def verify_one(tools, key, record, options, progress=None):
            verified.append(Path(key).stem)
            if verified[-1] == draining_stem:
                vidfunnel.interrupt.draining.set()
            return dict(record, status="verified", score=95)

        args = argparse.Namespace(paths=[str(self.root)], ffmpeg=None, quiet=True, verbose=False, log=None,
                                  yes=False, ssim_check=False, ssim_threshold=0.97, samples=None,
                                  stream=io.StringIO())
        with unittest.mock.patch("vidfunnel.deps.require", return_value=None), \
                unittest.mock.patch.object(verify_module, "FolderLock", side_effect=lock), \
                unittest.mock.patch.object(verify_module, "verify_record", side_effect=verify_one):
            run(args)
        return verified, locked

    def test_a_drain_in_the_last_record_of_a_folder_takes_no_further_folder_lock(self):
        self.assertEqual(self.verify_drained_in("harbour"), (["harbour"], ["first"]))
        self.assertEqual(self.tally(), (True, 1, 3))

    def test_a_drain_in_a_record_starts_no_further_record_of_its_folder(self):
        self.assertEqual(self.verify_drained_in("meadow"), (["harbour", "meadow"], ["first", "second"]))
        self.assertEqual(self.tally(), (True, 1, 2))

    def test_a_now_stop_file_stops_before_the_next_record(self):
        import vidfunnel.stopfile
        from vidfunnel.record import _write_json_atomic
        _write_json_atomic(vidfunnel.stopfile.stop_path(str(self.root / "second")),
                           {"host": "elsewhere", "requested": None, "mode": "now"})
        with unittest.mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                self.verify_drained_in(None)
        self.assertTrue(vidfunnel.stopfile.stopped_now)


def _set_at(record, path, value):
    target = record
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value


# Each entry: (field the reason names, [(path, value)]). The names point at 'elsewhere', a folder beside the
# record's, where a decoy source, output and record wait; or the record cannot be keyed or read at all.
REFUSED_NAMES = [
    ("source.name", [(["source", "name"], "../elsewhere/secret.avi")]),
    ("source.name", [(["source", "name"], "ABSOLUTE")]),
    ("source.name", [(["source", "name"], "..")]),
    ("source.name", [(["source", "name"], "")]),
    ("source.members", [(["source", "kind"], "vob-group"),
                        (["source", "members"], [{"name": "meadow.avi"}, {"name": "../elsewhere/secret.avi"}])]),
    ("source.members", [(["source", "kind"], "vob-group"), (["source", "members"], "meadow.avi")]),
    ("output.name", [(["output", "name"], "../elsewhere/secret.mp4")]),
    ("output.interim_name", [(["output", "interim_name"], "../elsewhere/secret.vidfunnel.mp4")]),
    ("output.sidecars", [(["output", "sidecars"], ["../elsewhere/secret.srt"])]),
    ("output", [(["output"], "meadow.mp4")]),
    ("stages", [(["stages"], "probe")]),
]


class VerifyRefusesNamesTest(unittest.TestCase):
    """A record whose names are not plain names in its folder is not verified and nothing it names is read.

    Hand-written records; the hash, fingerprint, ffprobe and ffmpeg calls verify makes are replaced by
    recorders (the hash and fingerprint still run on the file), so no ffmpeg is needed. 'harbour' is a good
    record in the same folder, 'meadow' the edited one, and 'elsewhere' a folder beside it with a decoy
    source, outputs and a converted record of its own named 'secret'.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        top = Path(self.temporary.name)
        self.folder = top / "library"
        self.elsewhere = top / "elsewhere"
        self.good_key = self.write_record(self.folder, "harbour")
        self.edited_key = self.write_record(self.folder, "meadow")
        self.decoy_key = self.write_record(self.elsewhere, "secret")
        for name in ("secret.vidfunnel.mp4", "secret.srt"):
            (self.elsewhere / name).write_bytes(b"decoy " * 64)
        self.calls = []

    def write_record(self, folder, stem):
        from vidfunnel.record import new_record
        folder.mkdir(exist_ok=True)
        source = folder / (stem + ".avi")
        source.write_bytes(b"hand-written source " * 64)
        (folder / (stem + ".mp4")).write_bytes(b"hand-written output " * 64)
        record = new_record(source)
        record["source"].update(kind="file", sha256="0123456789abcdef" + "0" * 48)
        record["output"]["name"] = stem + ".mp4"
        record["stages"] = {"probe": {"format": {"duration": 5.0},
                                      "video": {"index": 0, "fps": 25.0, "duration": 5.0}}}
        set_status(record, "converted")
        save_record(source, record)
        return source

    def edit(self, changes):
        record = load_record(self.edited_key)
        for path, value in changes:
            _set_at(record, path, str(self.elsewhere / "secret.avi") if value == "ABSOLUTE" else value)
        save_record(self.edited_key, record)
        return record

    def file_bytes(self):
        from vidfunnel.record import record_path
        return {key.stem: record_path(key).read_bytes() for key in (self.good_key, self.edited_key, self.decoy_key)}

    @contextlib.contextmanager
    def recorders(self):
        """Replace verify's file and tool calls with recorders; the hash and fingerprint still run."""
        import vidfunnel.verify as verify_module
        calls = self.calls
        real_hash, real_fingerprint = verify_module.sha256_file, verify_module.cheap_fingerprint

        def hashed(path, progress=None):
            calls.append(("sha256", str(path)))
            return real_hash(path)

        def fingerprinted(path):
            calls.append(("fingerprint", str(path)))
            return real_fingerprint(path)

        def probed(tools, path, *rest):
            calls.append(("ffprobe", str(path)))
            return {"format": {"duration": 5.0, "tags": {}}, "video": {"index": 0, "fps": 25.0, "duration": 5.0},
                    "audio": [], "subtitles": [], "streams": [{"type": "video"}]}

        def decoded(tools, path, progress=None, total_seconds=None):
            calls.append(("decode", str(path)))
            return {"returncode": 0, "classes": [], "ignored": [], "reason": "clean", "penalties": [], "flags": [],
                    "frames_decoded": 125}

        def counted(tools, path, index, progress=None):
            calls.append(("count", str(path)))
            return 125

        def ffmpeg(argv, check=True):
            calls.append(("ffmpeg", " ".join(str(part) for part in argv)))
            return types.SimpleNamespace(returncode=0, stderr="SSIM Y:0.990000 ")

        with unittest.mock.patch.object(verify_module, "sha256_file", hashed), \
                unittest.mock.patch.object(verify_module, "cheap_fingerprint", fingerprinted), \
                unittest.mock.patch.object(verify_module, "probe_file", probed), \
                unittest.mock.patch.object(verify_module, "decode_output", decoded), \
                unittest.mock.patch.object(verify_module, "count_frames", counted), \
                unittest.mock.patch.object(verify_module, "run_tool", ffmpeg):
            yield calls

    def run_verify(self):
        args = argparse.Namespace(paths=[str(self.folder)], ffmpeg=None, quiet=True, verbose=False, log=None,
                                  yes=False, ssim_check=True, ssim_threshold=0.97, samples=1, stream=io.StringIO())
        tools = types.SimpleNamespace(ffmpeg="ffmpeg-stand-in", ffprobe="ffprobe-stand-in")
        with unittest.mock.patch("vidfunnel.deps.require", return_value=tools), self.recorders():
            run(args)
        return args.stream.getvalue()

    def assert_only_the_good_record_was_read(self, text):
        self.assertTrue(self.calls, text)
        for what, target in self.calls:
            self.assertNotIn("meadow", target, (what, text))
            self.assertNotIn("secret", target, (what, text))
            self.assertNotIn(str(self.elsewhere), target, (what, text))
        self.assertIn(("decode", str(self.folder / "harbour.mp4")), self.calls)
        self.assertEqual(load_record(self.good_key)["status"], "verified", text)

    def test_run_refuses_each_shape_and_verifies_the_good_record(self):
        for field, changes in REFUSED_NAMES:
            with self.subTest(field=field, changes=changes):
                self.setUp()
                self.edit(changes)
                before = self.file_bytes()
                text = self.run_verify()
                self.assertRegex(text, r"skipping a record in '{}': {}(\[[0-9]+\](\.name)?)? is ".format(
                    re.escape(str(self.folder)), re.escape(field)))
                self.assertIn("1 skipped", text)
                after = self.file_bytes()
                self.assertEqual(after["meadow"], before["meadow"], "the edited record was written")
                self.assertEqual(after["secret"], before["secret"], "another folder's record was written")
                self.assert_only_the_good_record_was_read(text)

    def test_verify_record_refuses_each_shape_before_reading(self):
        import copy
        from vidfunnel.verify import RecordRefused
        options = types.SimpleNamespace(ssim_check=True, ssim_threshold=0.97, samples=1)
        for field, changes in REFUSED_NAMES:
            with self.subTest(field=field, changes=changes):
                self.setUp()
                record = self.edit(changes)
                kept = copy.deepcopy(record)
                before = self.file_bytes()
                with self.recorders(), self.assertRaises(RecordRefused) as caught:
                    verify_record(None, self.edited_key, record, options, quiet())
                self.assertIsInstance(caught.exception, ValueError)
                self.assertRegex(str(caught.exception), r"{}(\[[0-9]+\](\.name)?)? is ".format(re.escape(field)))
                self.assertEqual(record, kept)
                self.assertEqual(self.file_bytes(), before)
                self.assertEqual(self.calls, [])

    def test_a_record_edited_after_collection_is_skipped_under_the_lock(self):
        # The seam: FolderLock is wrapped so that, after run() has collected the records and before it takes
        # the folder's lock, the edited record's output name gains a directory part on disk.
        import vidfunnel.verify as verify_module
        from vidfunnel.record import FolderLock
        test = self

        class EditFirst(FolderLock):
            def __enter__(self):
                test.edit([(["output", "name"], "../elsewhere/secret.mp4")])
                test.before = test.file_bytes()
                return super().__enter__()

        with unittest.mock.patch.object(verify_module, "FolderLock", EditFirst):
            text = self.run_verify()
        self.assertIn("skipping 'meadow.avi': record of 'meadow.avi' is refused: output.name is "
                      "'../elsewhere/secret.mp4'", text)
        self.assertIn("1 skipped", text)
        after = self.file_bytes()
        self.assertEqual(after["meadow"], self.before["meadow"])
        self.assertEqual(after["secret"], self.before["secret"])
        self.assert_only_the_good_record_was_read(text)

    def test_a_file_path_beside_an_edited_record_verifies_its_own_record(self):
        # Given a file, run() matches the names of each record in its folder; an edited record's output that is
        # not an object, or a source name that is a list, must not stop that.
        self.edit([(["output"], "meadow.mp4"), (["source", "name"], ["meadow.avi"])])
        before = self.file_bytes()
        args = argparse.Namespace(paths=[str(self.folder / "harbour.mp4")], ffmpeg=None, quiet=True, verbose=False,
                                  log=None, yes=False, ssim_check=False, ssim_threshold=0.97, samples=None,
                                  stream=io.StringIO())
        tools = types.SimpleNamespace(ffmpeg="ffmpeg-stand-in", ffprobe="ffprobe-stand-in")
        with unittest.mock.patch("vidfunnel.deps.require", return_value=tools), self.recorders():
            run(args)
        text = args.stream.getvalue()
        self.assertEqual(self.file_bytes()["meadow"], before["meadow"])
        self.assert_only_the_good_record_was_read(text)

    def test_members_of_a_record_that_is_not_a_vob_group_are_not_read(self):
        # Only a VOB group's members are checked by name, so only a VOB group's members are inputs.
        self.edit([(["source", "members"], [{"name": "../elsewhere/secret.avi"}])])
        text = self.run_verify()
        self.assertNotIn("skipping", text)
        for what, target in self.calls:
            self.assertNotIn("secret", target, (what, text))
        self.assertIn(("count", str(self.folder / "meadow.avi")), self.calls)
        self.assertTrue(any(what == "ffmpeg" and str(self.folder / "meadow.avi") in target
                            for what, target in self.calls), self.calls)
        self.assertEqual(load_record(self.edited_key)["status"], "verified", text)


STOP_FIXTURES = (REMUX_FIXTURE, MP4_FIXTURE, SUBTITLE_FIXTURE)


@unittest.skipIf(TOOLS is None or not hasattr(os, "killpg"),
                 "ffmpeg not found on PATH (or no process groups); verify stop integration tests skipped")
class VerifyStopIntegrationTests(StopCase):
    """'vidfunnel verify' in a process of its own, stopped while its first record's decode runs.

    The --ffmpeg stand-in sleeps 3 s before each null decode and then runs
    the real ffmpeg, so the first record is still in progress when the
    signal or the stop file comes.
    """

    @classmethod
    def setUpClass(cls):
        cls.template = None
        if [name for name in STOP_FIXTURES if not os.path.exists(os.path.join(FIXTURE_DIR, name))]:
            return
        cls.template = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.template.cleanup)
        library = Path(cls.template.name) / "library"
        library.mkdir()
        convert_folder(library, STOP_FIXTURES)
        fake_bin = Path(cls.template.name) / "bin"
        fake_bin.mkdir()
        stand_in = fake_bin / "ffmpeg"
        stand_in.write_text('#!/bin/sh\ncase " $* " in *" -f null "*) sleep 3 ;; esac\nexec {} "$@"\n'.format(
            TOOLS.ffmpeg), encoding="utf-8")
        os.chmod(str(stand_in), 0o755)
        os.symlink(TOOLS.ffprobe, str(fake_bin / "ffprobe"))
        cls.stand_in = str(stand_in)

    def setUp(self):
        StopCase.setUp(self)
        if self.template is None:
            self.skipTest("fixtures missing; run tests/make_fixtures.sh")
        os.rmdir(str(self.root))
        shutil.copytree(str(Path(self.template.name) / "library"), str(self.root), symlinks=True)
        self.environment = dict(os.environ, PYTHONPATH=REPO_ROOT,
                                XDG_CACHE_HOME=str(Path(self.temporary.name) / "cache"))
        self.before = self.record_files()

    def record_files(self):
        folder = self.root / ".vidfunnel"
        return {path.name: path.read_bytes() for path in folder.glob("*.json") if path.name != "folder.json"}

    def run_stopped(self, act):
        import signal
        import subprocess
        import time
        process = subprocess.Popen([sys.executable, "-m", "vidfunnel", "--ffmpeg", self.stand_in, "verify",
                                    str(self.root)], cwd=REPO_ROOT, env=self.environment, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                                   start_new_session=True)
        self.addCleanup(lambda: process.poll() is None and os.killpg(process.pid, signal.SIGKILL))
        lines = []
        for line in process.stdout:
            lines.append(line)
            if "[verify 1/3]" in line:
                break
        else:
            process.wait(30)
            self.fail("'[verify 1/3]' never appeared; output:\n{}".format("".join(lines)))
        time.sleep(0.5)
        act(process)
        started = time.monotonic()
        rest, errors = process.communicate(timeout=120)
        return process.returncode, "".join(lines) + rest, errors, time.monotonic() - started

    def stop_all(self, *options):
        import subprocess
        result = subprocess.run([sys.executable, "-m", "vidfunnel", "stop-all"] + list(options) + [str(self.root)],
                                cwd=REPO_ROOT, env=self.environment, capture_output=True, universal_newlines=True,
                                timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def changed(self):
        after = self.record_files()
        self.assertEqual(sorted(after), sorted(self.before))
        self.assertFalse((self.root / ".vidfunnel" / "lock").exists(), "a lock was left behind")
        return sorted(name for name in after if after[name] != self.before[name])

    def test_verify_drained_by_sigusr1(self):
        import signal
        code, output, errors, _seconds = self.run_stopped(lambda process: os.kill(process.pid, signal.SIGUSR1))
        self.assertEqual(code, 0, "stdout:\n{}\nstderr:\n{}".format(output, errors))
        self.assertIn("vidfunnel: drained: finished 1 in progress, started no more (2 not started)", errors)
        changed = self.changed()
        self.assertEqual(len(changed), 1, changed)
        self.assertEqual(load_record(self.root / changed[0][:-len(".json")])["stages"]["verify"]["decode"]["returncode"],
                         0)

    def test_verify_drained_by_a_stop_file(self):
        code, output, errors, _seconds = self.run_stopped(lambda process: self.stop_all())
        self.assertEqual(code, 0, "stdout:\n{}\nstderr:\n{}".format(output, errors))
        self.assertIn("(drain): finishing the items in progress, starting no more", errors)
        # The first record's next ffprobe finds the file (check_now), so that record counts as in progress.
        self.assertIn("vidfunnel: drained: finished 1 in progress, started no more (2 not started)", errors)
        self.assertEqual(len(self.changed()), 1)

    def test_verify_stopped_by_a_now_stop_file(self):
        code, output, errors, seconds = self.run_stopped(lambda process: self.stop_all("--now"))
        self.assertEqual(code, 1, "stdout:\n{}\nstderr:\n{}".format(output, errors))
        self.assertIn("(now): stopping now", errors)
        self.assertIn("vidfunnel: stopped (a stop file asked for it)", errors)
        self.assertLess(seconds, 10.0)
        self.assertEqual(self.changed(), [], "a record was written by a verify that was stopped")


if __name__ == "__main__":
    unittest.main()

# End of File #
