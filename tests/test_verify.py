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

VERSION = "20261102"

import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
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


if __name__ == "__main__":
    unittest.main()

# End of File #
