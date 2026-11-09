"""Tests for vidfunnel.encode: temp names, skip rules, and convert runs on planned fixture copies.

The pure tests need no ffmpeg. The integration tests copy fixtures from
tests/fixtures into a temporary folder, plan them with vidfunnel.plan, and
convert the copies (never the fixture folder itself); they skip with a
message when ffmpeg or the fixtures are missing. Whether a converted record
ends at 'converted' or 'verified' depends on whether vidfunnel.verify is
present, so both are accepted.
"""

VERSION = "20261109"

import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

from vidfunnel.encode import (
    REFIT_DENOISE,
    convert_one,
    convert_paths,
    expected_duration,
    output_path_for,
    parse_crf,
    part_path,
    plan_ffmpeg_problem,
    record_key,
    refit_argv,
    refit_crf,
    refit_denoise,
    refit_one,
    refit_selection,
    run,
    skip_reason,
    write_par2,
)
from vidfunnel.ffmpeg import FfmpegError, ToolNotFound, current_identity, find_tools, plan_identity
from vidfunnel.ffmpeg import run as ffmpeg_run
from vidfunnel.plan import make_options, plan_paths
from vidfunnel.record import cheap_fingerprint, load_record, record_path, save_record, set_status

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(REPO_ROOT, "tests", "fixtures")
DONE = ("converted", "verified")
FIRST_RECORD_ID = "0f2b6c1e-4d55-4a8e-9c1d-2f3e4a5b6c7d"
SECOND_RECORD_ID = "a91c44d0-7e21-4b3f-8d6a-1c2b3d4e5f60"


def quiet():
    return types.SimpleNamespace(stream=io.StringIO(), quiet=True, verbose=False)


def convert_options(**values):
    defaults = dict(par2=None, verbose=False, jobs=1, dry_run=False, refit=False)
    defaults.update(values)
    return types.SimpleNamespace(**defaults)


def sha256_of(path):
    with open(str(path), "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def listing(root):
    """Every file and folder under root with its size, for 'nothing changed' checks."""
    found = {}
    for current, dir_names, file_names in os.walk(str(root)):
        for name in dir_names + file_names:
            path = os.path.join(current, name)
            found[os.path.relpath(path, str(root))] = os.path.getsize(path) if os.path.isfile(path) else None
    return found


# --- pure ---------------------------------------------------------------------------------


class NameTest(unittest.TestCase):

    def test_part_path_is_hidden_in_the_same_folder(self):
        self.assertEqual(part_path("/library/clips/movie.mp4", FIRST_RECORD_ID),
                         Path("/library/clips/.movie.mp4.0f2b6c1e.part"))
        self.assertEqual(part_path(Path("relative/movie.vidfunnel.mp4"), FIRST_RECORD_ID),
                         Path("relative/.movie.vidfunnel.mp4.0f2b6c1e.part"))

    def test_part_path_differs_per_record(self):
        first = part_path("/library/movie.mp4", FIRST_RECORD_ID)
        second = part_path("/library/movie.mp4", SECOND_RECORD_ID)
        self.assertNotEqual(first, second)
        self.assertEqual(second, Path("/library/.movie.mp4.a91c44d0.part"))

    def test_part_path_needs_a_record_id(self):
        for bad in (None, "", "xyz-12"):
            with self.assertRaises(ValueError):
                part_path("/library/movie.mp4", bad)

    def test_output_path_without_interim_name(self):
        record = {"output": {"name": "movie.mp4", "interim_name": None}}
        self.assertEqual(output_path_for(record, "/library"), Path("/library/movie.mp4"))

    def test_output_path_with_interim_name(self):
        record = {"output": {"name": "movie.mp4", "interim_name": "movie.vidfunnel.mp4"}}
        self.assertEqual(output_path_for(record, Path("/library")), Path("/library/movie.vidfunnel.mp4"))

    def test_output_path_without_any_name_raises(self):
        with self.assertRaises(ValueError):
            output_path_for({"id": "abc", "output": {"name": None}}, "/library")

    def test_record_key_per_source_kind(self):
        self.assertEqual(record_key("/disc", {"source": {"name": "movie.avi", "kind": "file"}}),
                         Path("/disc/movie.avi"))
        self.assertEqual(record_key("/disc", {"source": {"name": "VTS_01_1.VOB", "kind": "vob-group"}}),
                         Path("/disc/VTS_01_1.VOB"))
        self.assertEqual(record_key("/disc", {"source": {"name": "VIDEO_TS", "kind": "dvd-title",
                                                         "dvd_title": 3}}),
                         Path("/disc/VIDEO_TS_title03"))

    def test_expected_duration(self):
        self.assertEqual(expected_duration({"stages": {"probe": {"format": {"duration": 12.5}}}}), 12.5)
        self.assertEqual(expected_duration({"stages": {"probe": {"format": {"duration": "7.25"}}}}), 7.25)
        self.assertIsNone(expected_duration({"stages": {"probe": {"format": {"duration": None}}}}))
        self.assertIsNone(expected_duration({"stages": {"probe": {"format": {"duration": 0}}}}))
        self.assertIsNone(expected_duration({"stages": {}}))


class SkipReasonTest(unittest.TestCase):

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.output = Path(self.temporary.name) / "movie.mp4"
        self.output.write_bytes(b"converted bytes" * 100)

    def tearDown(self):
        self.temporary.cleanup()

    def record(self, status, fingerprint):
        return {"status": status, "output": {"name": "movie.mp4", "fingerprint": fingerprint}}

    def test_matching_fingerprint_is_already_converted(self):
        fingerprint = cheap_fingerprint(self.output)
        self.assertEqual(skip_reason(self.record("converted", fingerprint), self.output), "already converted")
        self.assertEqual(skip_reason(self.record("verified", fingerprint), self.output), "already converted")

    def test_mismatching_fingerprint_is_not_skipped(self):
        fingerprint = cheap_fingerprint(self.output)
        with open(str(self.output), "ab") as handle:
            handle.write(b"more")
        self.assertIsNone(skip_reason(self.record("converted", fingerprint), self.output))

    def test_missing_output_or_fingerprint_or_other_status(self):
        fingerprint = cheap_fingerprint(self.output)
        self.assertIsNone(skip_reason(self.record("planned", fingerprint), self.output))
        self.assertIsNone(skip_reason(self.record("converted", None), self.output))
        self.output.unlink()
        self.assertIsNone(skip_reason(self.record("converted", fingerprint), self.output))


class PlanFfmpegTest(unittest.TestCase):
    """plan_ffmpeg_problem against stand-in ffmpeg scripts that print a '-version' banner."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.ffmpeg = Path(self.temporary.name) / "ffmpeg"

    def banner(self, version, configuration):
        self.ffmpeg.write_text("#!/bin/sh\necho 'ffmpeg version {} Copyright (c) 2000-2026 the FFmpeg developers'\n"
                               "echo 'configuration: {}'\n".format(version, configuration))
        self.ffmpeg.chmod(self.ffmpeg.stat().st_mode | stat.S_IXUSR)

    def record(self, recorded):
        plan = {"argv": [str(self.ffmpeg), "-i", "in.avi", "-f", "mp4", "/library/out.mp4"]}
        if recorded is not None:
            plan["ffmpeg"] = recorded
        return {"stages": {"plan": plan}}

    def test_same_build_runs(self):
        self.banner("8.1.3", "--enable-gpl --enable-libx264")
        recorded = plan_identity(self.ffmpeg, {"version": "8.1.3", "configuration": ["--enable-gpl",
                                                                                      "--enable-libx264"]})
        self.assertEqual(recorded, current_identity(str(self.ffmpeg)))
        self.assertIsNone(plan_ffmpeg_problem(self.record(recorded)))

    def test_other_version_at_the_same_path_is_refused(self):
        recorded = plan_identity(self.ffmpeg, {"version": "8.1.3", "configuration": ["--enable-libx264"]})
        self.banner("7.1.5", "--enable-libx264")
        problem = plan_ffmpeg_problem(self.record(recorded))
        self.assertIn("ffmpeg 8.1.3 at '{}'".format(self.ffmpeg), problem)
        self.assertIn("'{}' here is ffmpeg 7.1.5".format(self.ffmpeg), problem)
        self.assertIn("plan again with --replan on this machine", problem)

    def test_same_version_with_another_configuration_is_refused(self):
        recorded = plan_identity(self.ffmpeg, {"version": "7.1.5", "configuration": ["--enable-libx264"]})
        self.banner("7.1.5", "--disable-gpl")
        problem = plan_ffmpeg_problem(self.record(recorded))
        self.assertIn("ffmpeg 7.1.5 at", problem)
        self.assertIn(recorded["configuration_sha256"][:12], problem)
        self.assertIn("plan again with --replan on this machine", problem)

    def test_two_records_naming_one_ffmpeg_ask_it_once(self):
        calls = self.ffmpeg.parent / "calls"
        self.ffmpeg.write_text("#!/bin/sh\necho call >> '{}'\n"
                               "echo 'ffmpeg version 7.1.5 Copyright (c) 2000-2026 the FFmpeg developers'\n"
                               "echo 'configuration: --enable-gpl'\n".format(calls))
        self.ffmpeg.chmod(self.ffmpeg.stat().st_mode | stat.S_IXUSR)
        recorded = plan_identity(self.ffmpeg, {"version": "7.1.5", "configuration": ["--enable-gpl"]})
        self.assertIsNone(plan_ffmpeg_problem(self.record(recorded)))
        self.assertIsNone(plan_ffmpeg_problem(self.record(recorded)))
        self.assertEqual(calls.read_text().splitlines(), ["call"])
        self.banner("8.1.3", "--enable-gpl")
        self.assertIn("here is ffmpeg 8.1.3", plan_ffmpeg_problem(self.record(recorded)))

    def test_record_without_the_field_runs_as_before(self):
        self.banner("7.1.5", "--disable-gpl")
        self.assertIsNone(plan_ffmpeg_problem(self.record(None)))

    def test_malformed_field_or_an_ffmpeg_that_does_not_run_is_refused(self):
        self.banner("7.1.5", "--disable-gpl")
        self.assertIn("not understood", plan_ffmpeg_problem(self.record("7.1.5")))
        self.ffmpeg.write_text("#!/bin/sh\nexit 3\n")
        recorded = plan_identity(self.ffmpeg, {"version": "7.1.5", "configuration": ["--disable-gpl"]})
        self.assertIn("does not run here", plan_ffmpeg_problem(self.record(recorded)))


# --- integration --------------------------------------------------------------------------


def _tools_or_none():
    try:
        return find_tools()
    except ToolNotFound:
        return None


TOOLS = _tools_or_none()
REMUX_SOURCE = "h264_high_mkv_aac.mkv"
REENCODE_SOURCE = "xvid_avi_mp3.avi"
MP4_SOURCE = "h264_mp4_aac.mp4"
SUBTITLE_SOURCE = "h264_mkv_srt_subtitles.mkv"
INTEGRATION_FIXTURES = (REMUX_SOURCE, REENCODE_SOURCE, MP4_SOURCE)


@unittest.skipIf(TOOLS is None, "ffmpeg not found on PATH; convert integration tests skipped")
class ConvertRunTest(unittest.TestCase):

    def setUp(self):
        missing = [name for name in INTEGRATION_FIXTURES if not os.path.exists(os.path.join(FIXTURE_DIR, name))]
        if missing:
            self.skipTest("fixtures missing ({}); run tests/make_fixtures.sh".format(", ".join(missing)))
        self.temporary = tempfile.TemporaryDirectory()
        # A cleanup, not tearDown, so cleanups added by a test (restoring permissions) run before it.
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "library"
        self.root.mkdir()

    def plan(self, *names):
        for name in names:
            shutil.copy2(os.path.join(FIXTURE_DIR, name), str(self.root / name))
        plan_paths(TOOLS, [str(self.root)], make_options(), progress=quiet())
        records = {}
        for name in names:
            record = load_record(self.root / name)
            self.assertIsNotNone(record, "plan wrote no record for '{}'".format(name))
            self.assertEqual(record["status"], "planned", "'{}' was not planned".format(name))
            records[name] = record
        return records

    def convert(self, name, record, **values):
        return convert_one(TOOLS, self.root / name, record, convert_options(**values), progress=quiet())

    def leftover_parts(self):
        return [name for name in os.listdir(str(self.root)) if name.endswith(".part")]

    def test_remux_writes_output_with_source_mtime_and_skips_on_second_run(self):
        record = self.plan(REMUX_SOURCE)[REMUX_SOURCE]
        self.assertEqual(record["action"], "remux")
        stage = self.convert(REMUX_SOURCE, record)
        output = self.root / "h264_high_mkv_aac.mp4"
        self.assertEqual(stage["outcome"], "ok", stage)
        self.assertTrue(output.is_file())
        self.assertEqual(os.stat(str(output)).st_mtime_ns, os.stat(str(self.root / REMUX_SOURCE)).st_mtime_ns)
        self.assertEqual(self.leftover_parts(), [])
        saved = load_record(self.root / REMUX_SOURCE)
        self.assertIn(saved["status"], DONE)
        for field in ("output_path", "part_path", "elapsed_s", "ffmpeg_stderr", "sidecars_written", "par2_files",
                      "output_size", "source_size", "size_ratio", "at"):
            self.assertIn(field, saved["stages"]["convert"], field)
        self.assertEqual(saved["stages"]["convert"]["output_path"], "h264_high_mkv_aac.mp4")
        self.assertEqual(saved["stages"]["convert"]["part_path"],
                         ".h264_high_mkv_aac.mp4.{}.part".format(record["id"][:8]))
        self.assertEqual(saved["output"]["size"], output.stat().st_size)
        self.assertEqual(saved["output"]["fingerprint"]["size"], output.stat().st_size)

        again = self.convert(REMUX_SOURCE, saved)
        self.assertEqual(again, {"outcome": "skipped", "skipped": "already converted"})

    def test_without_verify_module_the_record_stays_converted(self):
        record = self.plan(REMUX_SOURCE)[REMUX_SOURCE]
        progress = quiet()
        with unittest.mock.patch.dict(sys.modules, {"vidfunnel.verify": None}):
            stage = convert_one(TOOLS, self.root / REMUX_SOURCE, record, convert_options(), progress=progress)
        self.assertEqual(stage["outcome"], "ok", stage)
        self.assertEqual(load_record(self.root / REMUX_SOURCE)["status"], "converted")
        self.assertIn("verify is not available", progress.stream.getvalue())

    def test_stale_part_file_is_replaced(self):
        record = self.plan(REMUX_SOURCE)[REMUX_SOURCE]
        stale = self.root / ".h264_high_mkv_aac.mp4.{}.part".format(record["id"][:8])
        stale.write_bytes(b"left over from an interrupted run")
        other_id = SECOND_RECORD_ID if not record["id"].startswith(SECOND_RECORD_ID[:8]) else FIRST_RECORD_ID
        other = self.root / ".h264_high_mkv_aac.mp4.{}.part".format(other_id[:8])
        other.write_bytes(b"another record's part file")
        progress = quiet()
        stage = convert_one(TOOLS, self.root / REMUX_SOURCE, record, convert_options(), progress=progress)
        self.assertEqual(stage["outcome"], "ok", stage)
        self.assertFalse(stale.exists())
        self.assertIn("removed a stale", progress.stream.getvalue())
        self.assertEqual(other.read_bytes(), b"another record's part file")

    def test_reencode_output_decodes_as_h264(self):
        record = self.plan(REENCODE_SOURCE)[REENCODE_SOURCE]
        self.assertEqual(record["action"], "reencode")
        # The planned '-preset slower' takes long even on a small clip; the test swaps in 'ultrafast'
        # so it checks the convert mechanics, not x264's speed.
        argv = record["stages"]["plan"]["argv"]
        self.assertIn("slower", argv)
        record["stages"]["plan"]["argv"] = ["ultrafast" if part == "slower" else part for part in argv]
        save_record(self.root / REENCODE_SOURCE, record)
        stage = self.convert(REENCODE_SOURCE, record)
        self.assertEqual(stage["outcome"], "ok", stage)
        output = self.root / "xvid_avi_mp3.mp4"
        probe = subprocess.run([TOOLS.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=codec_name", "-of", "json", str(output)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(json.loads(probe.stdout)["streams"][0]["codec_name"], "h264")
        self.assertEqual(self.leftover_parts(), [])

    def test_mp4_source_writes_interim_name_and_leaves_source_alone(self):
        source = self.root / MP4_SOURCE
        record = self.plan(MP4_SOURCE)[MP4_SOURCE]
        size_before = source.stat().st_size
        hash_before = sha256_of(source)
        stage = self.convert(MP4_SOURCE, record)
        self.assertEqual(stage["outcome"], "ok", stage)
        self.assertTrue((self.root / "h264_mp4_aac.vidfunnel.mp4").is_file())
        self.assertEqual(source.stat().st_size, size_before)
        self.assertEqual(sha256_of(source), hash_before)
        self.assertEqual(stage["output_path"], "h264_mp4_aac.vidfunnel.mp4")

    def test_source_appended_after_plan_is_refused(self):
        record = self.plan(REMUX_SOURCE)[REMUX_SOURCE]
        with open(str(self.root / REMUX_SOURCE), "ab") as handle:
            handle.write(b"appended after planning")
        stage = self.convert(REMUX_SOURCE, record)
        self.assertEqual(stage["outcome"], "skipped")
        self.assertIn("source changed", stage["skipped"])
        saved = load_record(self.root / REMUX_SOURCE)
        self.assertEqual(saved["status"], "source-changed")
        self.assertIn("source-changed", saved["flags"])
        self.assertNotIn("convert", saved["stages"])
        self.assertFalse((self.root / "h264_high_mkv_aac.mp4").exists())

    def test_plan_records_the_ffmpeg_it_was_made_with(self):
        record = self.plan(REMUX_SOURCE)[REMUX_SOURCE]
        recorded = record["stages"]["plan"]["ffmpeg"]
        self.assertEqual(recorded, current_identity(TOOLS.ffmpeg))
        self.assertEqual(recorded["path"], record["stages"]["plan"]["argv"][0])
        self.assertRegex(recorded["configuration_sha256"], "^[0-9a-f]{64}$")

    def test_plan_made_with_another_ffmpeg_build_is_skipped_not_failed(self):
        record = self.plan(REMUX_SOURCE)[REMUX_SOURCE]
        record["stages"]["plan"]["ffmpeg"]["version"] = OTHER_BUILD_VERSION  # can match no real ffmpeg
        save_record(self.root / REMUX_SOURCE, record)
        record_bytes = record_path(self.root / REMUX_SOURCE).read_bytes()
        before = listing(self.root)
        progress = quiet()
        summary = convert_paths(TOOLS, [str(self.root)], convert_options(), progress)
        text = progress.stream.getvalue()
        self.assertEqual(summary.counts["skipped"], 1, text)
        self.assertEqual(summary.counts["failed"], 0, text)
        self.assertIn("the plan was made with ffmpeg {} at '{}'".format(OTHER_BUILD_VERSION, TOOLS.ffmpeg), text)
        self.assertIn("here is ffmpeg {}".format(current_identity(TOOLS.ffmpeg)["version"]), text)
        self.assertIn("plan again with --replan on this machine", text)
        self.assertEqual(record_path(self.root / REMUX_SOURCE).read_bytes(), record_bytes)
        self.assertEqual(listing(self.root), before)

    def test_plan_without_the_ffmpeg_field_converts_as_before(self):
        record = self.plan(REMUX_SOURCE)[REMUX_SOURCE]
        del record["stages"]["plan"]["ffmpeg"]
        save_record(self.root / REMUX_SOURCE, record)
        stage = self.convert(REMUX_SOURCE, record)
        self.assertEqual(stage["outcome"], "ok", stage)

    def test_bogus_codec_fails_with_stderr_and_no_part(self):
        record = self.plan(REMUX_SOURCE)[REMUX_SOURCE]
        argv = record["stages"]["plan"]["argv"]
        position = argv.index("-c:v") + 1
        argv[position] = "nonexistent_codec"
        stage = self.convert(REMUX_SOURCE, record)
        self.assertEqual(stage["outcome"], "failed")
        saved = load_record(self.root / REMUX_SOURCE)
        self.assertEqual(saved["status"], "failed")
        convert = saved["stages"]["convert"]
        self.assertIn("nonexistent_codec", convert["stderr"])
        self.assertNotEqual(convert["returncode"], 0)
        self.assertIn("nonexistent_codec", convert["argv"])
        self.assertTrue(convert["error"])
        self.assertEqual(self.leftover_parts(), [])
        self.assertFalse((self.root / "h264_high_mkv_aac.mp4").exists())

    def test_record_not_planned_is_refused_without_changes(self):
        record = self.plan(REMUX_SOURCE)[REMUX_SOURCE]
        set_status(record, "quarantined")
        save_record(self.root / REMUX_SOURCE, record)
        before = listing(self.root)
        record_bytes = record_path(self.root / REMUX_SOURCE).read_bytes()
        stage = self.convert(REMUX_SOURCE, record)
        self.assertEqual(stage["outcome"], "skipped")
        self.assertIn("quarantined", stage["skipped"])
        self.assertEqual(listing(self.root), before)
        self.assertEqual(record_path(self.root / REMUX_SOURCE).read_bytes(), record_bytes)

    def test_foreign_file_at_the_output_name_is_not_overwritten(self):
        record = self.plan(REMUX_SOURCE)[REMUX_SOURCE]
        foreign = self.root / "h264_high_mkv_aac.mp4"
        foreign.write_bytes(b"someone else's file")
        stage = self.convert(REMUX_SOURCE, record)
        self.assertEqual(stage["outcome"], "output-collision")
        self.assertEqual(foreign.read_bytes(), b"someone else's file")
        saved = load_record(self.root / REMUX_SOURCE)
        self.assertEqual(saved["status"], "failed")
        self.assertIn("output-collision", saved["flags"])

    def test_reencode_larger_than_source_is_kept_and_flagged(self):
        record = self.plan(REMUX_SOURCE)[REMUX_SOURCE]
        record["source"]["size"] = 1
        record["action"] = "reencode"  # the size rule applies to re-encodes; the argv itself is still the remux
        progress = quiet()
        stage = convert_one(TOOLS, self.root / REMUX_SOURCE, record, convert_options(), progress=progress)
        self.assertEqual(stage["outcome"], "ok", stage)
        self.assertEqual(stage["source_size"], 1)
        self.assertGreater(stage["output_size"], 1)
        output = self.root / "h264_high_mkv_aac.mp4"
        self.assertTrue(output.is_file())
        self.assertEqual(output.stat().st_size, stage["output_size"])
        self.assertEqual(self.leftover_parts(), [])
        saved = load_record(self.root / REMUX_SOURCE)
        self.assertIn(saved["status"], DONE)
        self.assertIn("output-larger", saved["flags"])
        self.assertNotIn("remux-larger", saved["flags"])
        self.assertEqual(saved["stages"]["convert"]["outcome"], "ok")
        self.assertIn("kept and flagged 'output-larger'", progress.stream.getvalue())

    def test_larger_reencode_counts_as_converted_in_the_summary(self):
        record = self.plan(REMUX_SOURCE)[REMUX_SOURCE]
        record["source"]["size"] = 1
        record["action"] = "reencode"
        save_record(self.root / REMUX_SOURCE, record)
        code, text = self.run_command(quiet=False)
        self.assertEqual(code, 0, text)
        summary_lines = [line for line in text.splitlines() if line.startswith("convert:")]
        self.assertEqual(len(summary_lines), 1, text)
        self.assertNotIn("larger", summary_lines[0])
        self.assertIn("0 failed", summary_lines[0])
        self.assertIn(load_record(self.root / REMUX_SOURCE)["status"], DONE, text)
        self.assertIn("output-larger", load_record(self.root / REMUX_SOURCE)["flags"])

    def test_keep_larger_option_is_gone(self):
        from vidfunnel import cli
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.build_parser().parse_args(["convert", "--keep-larger"])

    def test_remux_larger_than_source_is_kept_and_flagged(self):
        record = self.plan(REMUX_SOURCE)[REMUX_SOURCE]
        record["source"]["size"] = 1
        self.assertEqual(record["action"], "remux")
        stage = self.convert(REMUX_SOURCE, record)
        self.assertEqual(stage["outcome"], "ok", stage)
        self.assertTrue((self.root / "h264_high_mkv_aac.mp4").is_file())
        saved = load_record(self.root / REMUX_SOURCE)
        self.assertIn("remux-larger", saved["flags"])
        self.assertNotIn("output-larger", saved["flags"])

    def test_output_smaller_than_source_is_not_flagged(self):
        record = self.plan(REMUX_SOURCE)[REMUX_SOURCE]
        record["source"]["size"] = 10 ** 9
        record["action"] = "reencode"
        stage = self.convert(REMUX_SOURCE, record)
        self.assertEqual(stage["outcome"], "ok", stage)
        saved = load_record(self.root / REMUX_SOURCE)
        self.assertNotIn("output-larger", saved["flags"])
        self.assertNotIn("remux-larger", saved["flags"])

    def run_command(self, *extra, **values):
        defaults = dict(paths=[str(self.root)], ffmpeg=None, quiet=True, verbose=False, log=None, yes=False,
                        par2=None, jobs=1, dry_run=False)
        defaults.update(values)
        args = argparse.Namespace(**defaults)
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = run(args)
        return code, captured.getvalue()

    def test_dry_run_prints_argv_and_writes_nothing(self):
        self.plan(REMUX_SOURCE)
        before = listing(self.root)
        record_bytes = record_path(self.root / REMUX_SOURCE).read_bytes()
        code, text = self.run_command(dry_run=True)
        self.assertEqual(code, 0, text)
        self.assertIn("-c:v copy", text)
        record = load_record(self.root / REMUX_SOURCE)
        self.assertIn(".h264_high_mkv_aac.mp4.{}.part".format(record["id"][:8]), text)
        self.assertEqual(listing(self.root), before)
        self.assertEqual(record_path(self.root / REMUX_SOURCE).read_bytes(), record_bytes)

    def test_every_line_of_an_item_carries_its_counter_tag(self):
        record = self.plan(REMUX_SOURCE)[REMUX_SOURCE]
        progress = types.SimpleNamespace(stream=io.StringIO(), quiet=False, verbose=False)
        stage = convert_one(TOOLS, self.root / REMUX_SOURCE, record, convert_options(), progress, 3, 1054)
        self.assertEqual(stage["outcome"], "ok", stage)
        lines = progress.stream.getvalue().splitlines()
        for line in lines:
            self.assertTrue(line.startswith("[convert 3/1054] "), lines)
        self.assertIn("[convert 3/1054] remux '{}' -> 'h264_high_mkv_aac.mp4'".format(REMUX_SOURCE), lines)
        # The live lines of the remux and of the verify that follows it, and verify's one-shot lines.
        self.assertTrue(any(line.startswith("[convert 3/1054] [remux] ") for line in lines), lines)
        self.assertTrue(any(line.startswith("[convert 3/1054] [verify decode] ") for line in lines), lines)
        self.assertTrue(any(line.startswith("[convert 3/1054] verifying 'h264_high_mkv_aac.mp4'")
                            for line in lines), lines)
        self.assertTrue(any(line.startswith("[convert 3/1054]   converted '") for line in lines), lines)

    def test_marked_outputs_go_through_interim_names_until_retire(self):
        # Without --mark-outputs the names are unchanged: test_remux_writes_output_with_source_mtime_and_skips_on_
        # second_run (clean name), test_mp4_source_writes_interim_name_and_leaves_source_alone (the MP4 case).
        from vidfunnel.report import collect, rows_from_records
        from vidfunnel.retire import retire_one
        if not os.path.exists(os.path.join(FIXTURE_DIR, SUBTITLE_SOURCE)):
            self.skipTest("fixture '{}' missing; run tests/make_fixtures.sh".format(SUBTITLE_SOURCE))
        shutil.copy2(os.path.join(FIXTURE_DIR, SUBTITLE_SOURCE), str(self.root / "Movie night.mkv"))
        shutil.copy2(os.path.join(FIXTURE_DIR, REMUX_SOURCE), str(self.root / REMUX_SOURCE))
        plan_paths(TOOLS, [str(self.root)], make_options(mark_outputs=True), progress=quiet())
        code, text = self.run_command()
        self.assertEqual(code, 0, text)
        self.assertEqual(sorted(name for name in os.listdir(str(self.root)) if not name.startswith(".")),
                         ["Movie night.mkv", "Movie night.vidfunnel.mp4", "Movie night.vidfunnel.srt",
                          REMUX_SOURCE, "h264_high_mkv_aac.vidfunnel.mp4"])
        record = load_record(self.root / "Movie night.mkv")
        self.assertEqual(record["status"], "verified", text)
        self.assertEqual((record["output"]["name"], record["output"]["interim_name"]),
                         ("Movie night.mp4", "Movie night.vidfunnel.mp4"))
        self.assertEqual((record["output"]["sidecars"], record["output"]["interim_sidecars"]),
                         (["Movie night.srt"], ["Movie night.vidfunnel.srt"]))
        self.assertEqual(record["stages"]["convert"]["output_path"], "Movie night.vidfunnel.mp4")
        self.assertEqual(record["stages"]["convert"]["sidecars_written"], ["Movie night.vidfunnel.srt"])
        self.assertEqual(record["stages"]["verify"]["output_name"], "Movie night.vidfunnel.mp4")
        rows = {row["path"].rsplit("/", 1)[-1]: row for row in rows_from_records(collect([str(self.root)]))}
        self.assertEqual(rows["Movie night.mkv"]["output_name"], "Movie night.vidfunnel.mp4")

        stage = retire_one(self.root / "Movie night.mkv", record, self.root, quiet=True)
        self.assertTrue(stage["renamed_output"])
        self.assertEqual(stage["renamed_sidecars"], ["Movie night.srt"])
        self.assertEqual(sorted(name for name in os.listdir(str(self.root)) if not name.startswith(".")),
                         ["Movie night.mp4", "Movie night.srt", REMUX_SOURCE, "h264_high_mkv_aac.vidfunnel.mp4"])
        self.assertIn("-->", (self.root / "Movie night.srt").read_text(errors="replace"))
        retired = load_record(self.root / "Movie night.mkv")
        self.assertEqual(retired["status"], "retired")
        rows = {row["path"].rsplit("/", 1)[-1]: row for row in rows_from_records(collect([str(self.root)]))}
        self.assertEqual(rows["Movie night.mkv"]["output_name"], "Movie night.mp4")
        self.assertEqual(rows[REMUX_SOURCE]["output_name"], "h264_high_mkv_aac.vidfunnel.mp4")

    def test_two_jobs_tag_every_item_line(self):
        self.plan(REMUX_SOURCE, MP4_SOURCE)
        code, text = self.run_command(jobs=2, quiet=False)
        self.assertEqual(code, 0, text)
        tags = set()
        for line in text.splitlines():
            if line.startswith("[convert "):
                tags.add(line.split("]", 1)[0] + "]")
            else:
                self.assertTrue(line.startswith(("scanning ", "convert: ")), text)
        self.assertEqual(tags, {"[convert 1/2]", "[convert 2/2]"}, text)
        for tag in tags:
            self.assertIn(tag + " [remux] ", text)

    def test_two_jobs_convert_both_planned_records(self):
        self.plan(REMUX_SOURCE, MP4_SOURCE)
        code, text = self.run_command(jobs=2)
        self.assertEqual(code, 0, text)
        self.assertTrue((self.root / "h264_high_mkv_aac.mp4").is_file())
        self.assertTrue((self.root / "h264_mp4_aac.vidfunnel.mp4").is_file())
        for name in (REMUX_SOURCE, MP4_SOURCE):
            self.assertIn(load_record(self.root / name)["status"], DONE, text)
        self.assertFalse((self.root / ".vidfunnel" / "lock").exists())
        self.assertEqual(self.leftover_parts(), [])

    def test_planned_records_with_one_output_name_are_both_refused(self):
        records = self.plan(REMUX_SOURCE, REENCODE_SOURCE)
        clash = records[REENCODE_SOURCE]
        clash["output"]["name"] = "h264_high_mkv_aac.mp4"
        clash["stages"]["plan"]["argv"][-1] = str(self.root / "h264_high_mkv_aac.mp4")
        save_record(self.root / REENCODE_SOURCE, clash)
        before = listing(self.root)
        record_bytes = {name: record_path(self.root / name).read_bytes() for name in (REMUX_SOURCE, REENCODE_SOURCE)}
        progress = quiet()
        summary = convert_paths(TOOLS, [str(self.root)], convert_options(jobs=2), progress)
        text = progress.stream.getvalue()
        self.assertEqual(summary.counts.get("collision"), 2, text)
        self.assertEqual(summary.counts["converted"] + summary.counts["verified"], 0, text)
        self.assertIn("'{}'".format(REMUX_SOURCE), text)
        self.assertIn("'{}'".format(REENCODE_SOURCE), text)
        self.assertEqual(listing(self.root), before)
        for name, content in record_bytes.items():
            self.assertEqual(record_path(self.root / name).read_bytes(), content, name)
        code, _text = self.run_command()
        self.assertEqual(code, 1)

    def test_read_only_folder_is_skipped_without_a_traceback(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("running as root; a read-only folder is still writable")
        self.plan(REMUX_SOURCE)
        record_folder = self.root / ".vidfunnel"
        for folder in (record_folder, self.root):
            os.chmod(str(folder), 0o555)
            self.addCleanup(os.chmod, str(folder), 0o755)
        progress = quiet()
        summary = convert_paths(TOOLS, [str(self.root)], convert_options(), progress)
        self.assertEqual(summary.counts["skipped"], 1, progress.stream.getvalue())
        self.assertEqual(summary.lock_errors, [str(self.root)])
        self.assertIn("cannot take its lock", progress.stream.getvalue())
        self.assertFalse((self.root / "h264_high_mkv_aac.mp4").exists())
        code, _text = self.run_command()
        self.assertEqual(code, 1)

    @unittest.skipIf(shutil.which("par2") is None, "par2 not on PATH; PAR2 test skipped")
    def test_par2_override_writes_verifiable_files(self):
        record = self.plan(REMUX_SOURCE)[REMUX_SOURCE]
        stage = self.convert(REMUX_SOURCE, record, par2=10)
        self.assertEqual(stage["outcome"], "ok", stage)
        self.assertIn("h264_high_mkv_aac.mp4.par2", stage["par2_files"])
        self.assertTrue(any(".vol" in name for name in stage["par2_files"]), stage["par2_files"])
        for name in stage["par2_files"]:
            self.assertTrue((self.root / name).is_file(), name)


# --- refit: pure --------------------------------------------------------------------------

PLANNED_ARGV = ["/usr/bin/ffmpeg", "-hide_banner", "-i", "/library/clip.avi", "-map", "0:v:0", "-map", "0:a?",
                "-vf", "setparams=range=tv", "-c:v", "libx264", "-preset", "slower", "-crf", "18",
                "-profile:v", "high", "-c:a:0", "aac", "-filter:a:0", "aresample=async=1",
                "-metadata", "vidfunnel={}", "-f", "mp4", "/library/clip.mp4"]


def refit_record(**changes):
    record = {"status": "verified", "action": "reencode", "flags": ["output-larger"], "stages": {}}
    record.update(changes)
    return record


class RefitRuleTest(unittest.TestCase):

    def test_parse_crf(self):
        self.assertEqual(parse_crf(PLANNED_ARGV), 18)
        self.assertIsNone(parse_crf(["ffmpeg", "-c:v", "copy", "out.mp4"]))
        self.assertIsNone(parse_crf(["ffmpeg", "-crf", "eighteen", "out.mp4"]))
        self.assertIsNone(parse_crf(["ffmpeg", "-crf"]))

    def test_crf_steps_follow_the_size_ratio(self):
        # ceil(ln(ratio) / ln(1.12)): 1.05 -> 0.43 -> 1 step; 1.08 -> 0.68 -> 1; 1.42 -> 3.09 -> 4 steps;
        # 2.5 -> 8.09 -> 9 steps (then capped).
        self.assertEqual(refit_crf(18, 1.05, 480), (19, 23, 1))
        self.assertEqual(refit_crf(18, 1.08, 480), (19, 23, 1))
        self.assertEqual(refit_crf(18, 1.42, 480), (22, 23, 4))
        self.assertEqual(refit_crf(20, 1.42, 1080), (24, 24, 4))

    def test_crf_caps_by_height(self):
        self.assertEqual(refit_crf(18, 2.5, 480), (23, 23, 9))
        self.assertEqual(refit_crf(20, 2.5, 1080), (24, 24, 9))
        self.assertEqual(refit_crf(18, 2.5, 576)[1], 23)
        self.assertEqual(refit_crf(19, 2.5, 577)[1], 24)
        self.assertEqual(refit_crf(18, 2.5, None)[1], 23)

    def test_ratio_not_above_one_gives_no_step(self):
        self.assertEqual(refit_crf(18, 1.0, 480), (18, 23, 0))
        self.assertEqual(refit_crf(18, 0.8, 480), (18, 23, 0))
        self.assertEqual(refit_crf(23, 1.42, 480), (23, 23, 4))

    def test_argv_with_a_filter_chain_and_denoise(self):
        argv, chain = refit_argv(PLANNED_ARGV, 21, True, "/library/.clip.mp4.0f2b6c1e.part")
        self.assertEqual(chain, REFIT_DENOISE + ",setparams=range=tv")
        expected = list(PLANNED_ARGV)
        expected[expected.index("-crf") + 1] = "21"
        expected[expected.index("-vf") + 1] = chain
        expected[-1] = "/library/.clip.mp4.0f2b6c1e.part"
        self.assertEqual(argv, expected)
        self.assertEqual(PLANNED_ARGV[PLANNED_ARGV.index("-crf") + 1], "18", "the planned argv must not change")

    def test_argv_with_a_filter_chain_without_denoise(self):
        argv, chain = refit_argv(PLANNED_ARGV, 21, False, "/library/part")
        self.assertEqual(chain, "setparams=range=tv")
        self.assertEqual(argv[argv.index("-vf") + 1], "setparams=range=tv")
        self.assertEqual(argv[argv.index("-crf") + 1], "21")
        self.assertEqual(argv[:-1][:argv.index("-crf")], PLANNED_ARGV[:PLANNED_ARGV.index("-crf")])

    def test_argv_without_a_filter_chain(self):
        bare = [item for position, item in enumerate(PLANNED_ARGV)
                if item != "-vf" and (position == 0 or PLANNED_ARGV[position - 1] != "-vf")]
        argv, chain = refit_argv(bare, 20, True, "/library/part")
        self.assertEqual(chain, REFIT_DENOISE)
        self.assertEqual(argv[argv.index("-c:v") - 2:argv.index("-c:v")], ["-vf", REFIT_DENOISE])
        self.assertEqual(argv.count("-vf"), 1)
        argv, chain = refit_argv(bare, 20, False, "/library/part")
        self.assertIsNone(chain)
        self.assertNotIn("-vf", argv)
        self.assertEqual(len(argv), len(bare))

    def test_argv_with_filter_v_spelling(self):
        spelled = ["-filter:v" if item == "-vf" else item for item in PLANNED_ARGV]
        argv, chain = refit_argv(spelled, 20, True, "/library/part")
        self.assertEqual(argv[argv.index("-filter:v") + 1], REFIT_DENOISE + ",setparams=range=tv")
        self.assertNotIn("-vf", argv)
        self.assertEqual(argv[argv.index("-filter:a:0") + 1], "aresample=async=1")

    def test_denoise_goes_after_a_cadence_filter(self):
        chained = list(PLANNED_ARGV)
        chained[chained.index("-vf") + 1] = "fieldmatch,decimate,setparams=range=tv"
        _argv, chain = refit_argv(chained, 20, True, "/library/part", cadence_filter="fieldmatch,decimate")
        self.assertEqual(chain, "fieldmatch,decimate," + REFIT_DENOISE + ",setparams=range=tv")

    def test_argv_without_crf_is_refused(self):
        with self.assertRaises(ValueError):
            refit_argv(["ffmpeg", "-c:v", "copy", "-f", "mp4", "out.mp4"], 20, False, "part")

    def test_selection(self):
        self.assertIsNone(refit_selection(refit_record()))
        self.assertIsNone(refit_selection(refit_record(status="converted")))
        self.assertEqual(refit_selection(refit_record(status="planned")), "not converted")
        self.assertEqual(refit_selection(refit_record(action="remux")), "not a re-encode")
        self.assertEqual(refit_selection(refit_record(flags=[])), "not larger")
        self.assertEqual(refit_selection(refit_record(flags=["output-larger", "fit-capped"])), "already capped")
        self.assertEqual(refit_selection(refit_record(stages={"refit": {"outcome": "replaced"}})),
                         "already refitted")
        self.assertIsNone(refit_selection(refit_record(stages={"refit": {"outcome": "failed"}})))

    def test_denoise_needs_heavy_class_and_high_blockiness(self):
        def with_analysis(klass, score):
            return refit_record(stages={"analyze": {"size_class": {"class": klass, "bits_per_pixel": 0.1},
                                                    "blockiness": {"score": score}}})
        self.assertTrue(refit_denoise(with_analysis("heavy", 3.5))[0])
        self.assertFalse(refit_denoise(with_analysis("heavy", 3.4999))[0])
        self.assertFalse(refit_denoise(with_analysis("near", 200.0))[0])
        self.assertFalse(refit_denoise(with_analysis("heavy", None))[0])
        denoise, reason = refit_denoise(refit_record())
        self.assertFalse(denoise)
        self.assertIn("planned before", reason)


# --- refit: integration --------------------------------------------------------------------

OTHER_BUILD_VERSION = "0.0.0-other-build"  # the CI container's ffmpeg is 8.1.3, so no real version may be used here
REFIT_SOURCE = "h264_avi_mp3.avi"       # re-encoded with --reencode-video it comes out larger than the source
REFIT_OUTPUT = "h264_avi_mp3.mp4"


@unittest.skipIf(TOOLS is None, "ffmpeg not found on PATH; refit integration tests skipped")
class RefitRunTest(unittest.TestCase):

    def setUp(self):
        missing = [name for name in (REFIT_SOURCE, REENCODE_SOURCE, REMUX_SOURCE)
                   if not os.path.exists(os.path.join(FIXTURE_DIR, name))]
        if missing:
            self.skipTest("fixtures missing ({}); run tests/make_fixtures.sh".format(", ".join(missing)))
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "library"
        self.root.mkdir()

    def plan_and_convert(self, *names, **plan_values):
        for name in names:
            shutil.copy2(os.path.join(FIXTURE_DIR, name), str(self.root / name))
        plan_paths(TOOLS, [str(self.root)], make_options(**plan_values), progress=quiet())
        progress = quiet()
        convert_paths(TOOLS, [str(self.root)], convert_options(), progress)
        return {name: load_record(self.root / name) for name in names}

    def refit(self, **values):
        progress = quiet()
        summary = convert_paths(TOOLS, [str(self.root)], convert_options(refit=True, **values), progress)
        return summary, progress.stream.getvalue()

    def leftover_parts(self):
        return [name for name in os.listdir(str(self.root)) if name.endswith(".part")]

    def test_larger_reencode_is_refitted_replaced_and_verified(self):
        records = self.plan_and_convert(REFIT_SOURCE, REENCODE_SOURCE, reencode_video=True)
        record = records[REFIT_SOURCE]
        self.assertIn("output-larger", record["flags"], "the fixture no longer re-encodes larger; pick another")
        self.assertNotIn("output-larger", records[REENCODE_SOURCE]["flags"])
        self.assertEqual(record["stages"]["plan"]["size_class"]["class"], "heavy")
        self.assertGreaterEqual(record["stages"]["plan"]["blockiness"]["score"], 3.5)
        source_size = (self.root / REFIT_SOURCE).stat().st_size
        before_size = (self.root / REFIT_OUTPUT).stat().st_size
        ratio = before_size / float(source_size)
        self.assertGreater(ratio, 1.0)

        summary, text = self.refit()
        self.assertEqual(summary.counts["refitted"], 1, text)
        self.assertEqual(summary.counts["kept"], 0, text)
        self.assertEqual(summary.counts["failed"], 0, text)
        self.assertEqual(summary.skip_reasons, {"not larger": 1}, text)
        self.assertIn("[refit 1/1] '{}': CRF 18 -> 19, hqdn3d, ratio {:.2f}".format(REFIT_SOURCE, ratio), text)
        self.assertEqual(self.leftover_parts(), [])

        saved = load_record(self.root / REFIT_SOURCE)
        stage = saved["stages"]["refit"]
        self.assertEqual(stage["outcome"], "replaced")
        attempt = stage["attempt"]
        self.assertEqual((attempt["crf"], attempt["base_crf"], attempt["denoise"]), (19, 18, True))
        self.assertTrue(attempt["filters"].startswith(REFIT_DENOISE + ","), attempt["filters"])
        after_size = (self.root / REFIT_OUTPUT).stat().st_size
        self.assertLess(after_size, before_size)
        self.assertEqual(attempt["output_size"], after_size)
        self.assertIn("refitted", saved["flags"])
        self.assertEqual("output-larger" in saved["flags"], after_size > source_size)
        self.assertEqual(saved["output"]["size"], after_size)
        self.assertEqual(saved["status"], "verified", text)
        self.assertEqual(saved["output"]["sha256"], sha256_of(self.root / REFIT_OUTPUT))
        self.assertEqual(len(saved["stages"]["verify_history"]), 1)
        self.assertEqual(saved["stages"]["verify"]["size"]["output"], after_size)
        self.assertEqual(saved["stages"]["verify"]["identity"]["ok"], True)

        from vidfunnel.verify import verify_record
        again = verify_record(TOOLS, self.root / REFIT_SOURCE, saved, None, progress=quiet())
        self.assertEqual(again["status"], "verified")
        self.assertEqual(again["score"], saved["score"])

        summary, text = self.refit()
        self.assertEqual(summary.counts["refitted"] + summary.counts["kept"], 0, text)
        self.assertEqual(summary.skip_reasons, {"already refitted": 1, "not larger": 1}, text)

    def test_refit_of_a_plan_made_with_another_ffmpeg_build_is_skipped(self):
        records = self.plan_and_convert(REFIT_SOURCE, reencode_video=True)
        record = records[REFIT_SOURCE]
        self.assertIn("output-larger", record["flags"], "the fixture no longer re-encodes larger; pick another")
        record["stages"]["plan"]["ffmpeg"]["configuration_sha256"] = "0" * 64
        save_record(self.root / REFIT_SOURCE, record)
        record_bytes = record_path(self.root / REFIT_SOURCE).read_bytes()
        output_hash = sha256_of(self.root / REFIT_OUTPUT)
        summary, text = self.refit()
        self.assertEqual(summary.counts["refitted"] + summary.counts["kept"] + summary.counts["failed"], 0, text)
        self.assertEqual(summary.counts["skipped"], 1, text)
        self.assertIn("(configuration 000000000000), but '{}' here is ffmpeg".format(TOOLS.ffmpeg), text)
        self.assertIn("plan again with --replan on this machine", text)
        self.assertEqual(record_path(self.root / REFIT_SOURCE).read_bytes(), record_bytes)
        self.assertEqual(sha256_of(self.root / REFIT_OUTPUT), output_hash)

        del record["stages"]["plan"]["ffmpeg"]
        save_record(self.root / REFIT_SOURCE, record)
        with unittest.mock.patch("vidfunnel.encode._verify", side_effect=lambda tools, key, record, *rest: record):
            summary, text = self.refit()
        self.assertEqual(summary.counts["refitted"], 1, "a record without stages.plan.ffmpeg refits as before\n" + text)

    def test_refit_of_a_marked_output_replaces_the_interim_file(self):
        records = self.plan_and_convert(REFIT_SOURCE, reencode_video=True, mark_outputs=True)
        record = records[REFIT_SOURCE]
        interim = self.root / "h264_avi_mp3.vidfunnel.mp4"
        self.assertEqual(record["output"]["interim_name"], interim.name)
        self.assertIn("output-larger", record["flags"])
        before = interim.stat().st_size
        summary, text = self.refit()
        self.assertEqual(summary.counts["refitted"], 1, text)
        self.assertLess(interim.stat().st_size, before)
        self.assertFalse((self.root / REFIT_OUTPUT).exists())
        self.assertEqual(self.leftover_parts(), [])
        saved = load_record(self.root / REFIT_SOURCE)
        self.assertEqual(saved["status"], "verified", text)
        self.assertEqual(saved["stages"]["verify"]["output_name"], interim.name)
        self.assertEqual(saved["output"]["size"], interim.stat().st_size)

    def test_every_line_of_a_refit_carries_its_counter_tag(self):
        self.plan_and_convert(REFIT_SOURCE, reencode_video=True)
        key = self.root / REFIT_SOURCE
        progress = types.SimpleNamespace(stream=io.StringIO(), quiet=False, verbose=False)
        outcome, reason = refit_one(TOOLS, key, load_record(key), convert_options(refit=True), progress, 2, 7)
        lines = progress.stream.getvalue().splitlines()
        self.assertEqual(outcome, "refitted", lines)
        for line in lines:
            self.assertTrue(line.startswith("[refit 2/7] "), lines)
        self.assertTrue(any(line.startswith("[refit 2/7] '{}': CRF 18 -> ".format(REFIT_SOURCE)) for line in lines),
                        lines)
        self.assertTrue(any(line.startswith("[refit 2/7] [refit] ") for line in lines), lines)
        self.assertTrue(any(line.startswith("[refit 2/7] [verify decode] ") for line in lines), lines)

    def test_reset_record_goes_through_a_separate_verify_run(self):
        self.plan_and_convert(REFIT_SOURCE, reencode_video=True)
        with unittest.mock.patch("vidfunnel.encode._verify", side_effect=lambda tools, key, record, *rest: record):
            summary, text = self.refit()
        self.assertEqual(summary.counts["refitted"], 1, text)
        saved = load_record(self.root / REFIT_SOURCE)
        self.assertEqual(saved["status"], "converted")
        self.assertNotIn("sha256", saved["output"])
        self.assertNotIn("verify", saved["stages"])
        self.assertNotIn("score", saved)
        from vidfunnel import verify
        args = argparse.Namespace(paths=[str(self.root)], ffmpeg=None, quiet=True, verbose=False, ssim_check=False,
                                  ssim_threshold=0.97, samples=None, stream=io.StringIO())
        with contextlib.redirect_stdout(io.StringIO()):
            code = verify.run(args)
        self.assertEqual(code, 0, args.stream.getvalue())
        verified = load_record(self.root / REFIT_SOURCE)
        self.assertEqual(verified["status"], "verified")
        self.assertEqual(verified["output"]["sha256"], sha256_of(self.root / REFIT_OUTPUT))

    def test_interrupt_during_par2_leaves_a_record_that_describes_the_new_output(self):
        if shutil.which("par2") is None:
            self.skipTest("par2 is not on PATH")
        self.plan_and_convert(REFIT_SOURCE, reencode_video=True, par2=10)
        before = load_record(self.root / REFIT_SOURCE)
        self.assertTrue(before["stages"]["convert"]["par2_files"])
        with unittest.mock.patch("vidfunnel.encode.write_par2", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.refit()
        saved = load_record(self.root / REFIT_SOURCE)
        on_disk = (self.root / REFIT_OUTPUT).stat().st_size
        self.assertEqual(saved["stages"]["refit"]["outcome"], "replaced")
        self.assertEqual(saved["stages"]["refit"]["attempt"]["output_size"], on_disk)
        self.assertEqual(saved["output"]["size"], on_disk)
        self.assertEqual(saved["status"], "converted")
        self.assertNotIn("sha256", saved["output"])
        self.assertIn("refitted", saved["flags"])
        self.assertIn("par2-stale", saved["flags"], "the convert PAR2 set no longer matches the output")
        self.assertEqual(self.leftover_parts(), [])
        summary, text = self.refit()
        self.assertEqual(summary.skip_reasons, {"already refitted": 1}, text)

    def test_refit_without_par2_on_path_removes_the_stale_set(self):
        if shutil.which("par2") is None:
            self.skipTest("par2 is not on PATH")
        self.plan_and_convert(REFIT_SOURCE, reencode_video=True, par2=10)
        output = self.root / REFIT_OUTPUT
        self.assertTrue([name for name in os.listdir(str(self.root)) if name.endswith(".par2")])
        real_which = shutil.which
        with unittest.mock.patch("vidfunnel.encode.shutil.which",
                                 side_effect=lambda name, *rest: None if name == "par2" else real_which(name, *rest)):
            summary, text = self.refit()
        self.assertEqual(summary.counts["refitted"], 1, text)
        self.assertEqual([name for name in os.listdir(str(self.root)) if name.endswith(".par2")], [])
        saved = load_record(self.root / REFIT_SOURCE)
        self.assertIn("par2-stale", saved["flags"])
        self.assertNotIn("par2-skipped", saved["flags"])
        self.assertIn("stale set is removed", text)

    def test_refit_rewrites_par2_and_clears_the_stale_flag(self):
        if shutil.which("par2") is None:
            self.skipTest("par2 is not on PATH")
        self.plan_and_convert(REFIT_SOURCE, reencode_video=True, par2=10)
        summary, text = self.refit()
        self.assertEqual(summary.counts["refitted"], 1, text)
        saved = load_record(self.root / REFIT_SOURCE)
        self.assertNotIn("par2-stale", saved["flags"])
        self.assertTrue(saved["stages"]["refit"]["par2_files"])
        self.assertEqual(subprocess.run(["par2", "verify", "-q", str(self.root / (REFIT_OUTPUT + ".par2"))],
                                        capture_output=True).returncode, 0)

    def test_refit_not_smaller_keeps_the_output_and_retire_refuses_it(self):
        # Constructed: the planned CRF is lowered to 10, so the refit at CRF 11 comes out larger
        # than the existing CRF 18 output.
        record = self.plan_and_convert(REFIT_SOURCE, reencode_video=True)[REFIT_SOURCE]
        argv = record["stages"]["plan"]["argv"]
        argv[argv.index("-crf") + 1] = "10"
        save_record(self.root / REFIT_SOURCE, record)
        output_hash = sha256_of(self.root / REFIT_OUTPUT)
        summary, text = self.refit()
        self.assertEqual(summary.counts["kept"], 1, text)
        self.assertEqual(summary.counts["refitted"], 0, text)
        self.assertEqual(sha256_of(self.root / REFIT_OUTPUT), output_hash)
        self.assertEqual(self.leftover_parts(), [])
        saved = load_record(self.root / REFIT_SOURCE)
        self.assertEqual(saved["status"], "verified")
        self.assertEqual(saved["output"]["sha256"], output_hash)
        self.assertIn("fit-capped", saved["flags"])
        self.assertIn("output-larger", saved["flags"])
        self.assertNotIn("refitted", saved["flags"])
        stage = saved["stages"]["refit"]
        self.assertEqual(stage["outcome"], "kept-existing")
        self.assertIn("not smaller than the existing output", stage["reason"])
        self.assertEqual(stage["attempt"]["crf"], 11)
        self.assertGreater(stage["attempt"]["output_size"], saved["output"]["size"])
        self.assertIn("kept the existing output, flagged 'fit-capped'", text)

        summary, text = self.refit()
        self.assertEqual(summary.skip_reasons, {"already capped": 1}, text)

        from vidfunnel.retire import RetireRefused, check_eligible
        with self.assertRaises(RetireRefused) as caught:
            check_eligible(self.root / REFIT_SOURCE, saved, quiet=True)
        self.assertEqual(caught.exception.reason, "output-larger")
        self.assertTrue(check_eligible(self.root / REFIT_SOURCE, saved, quiet=True, allow_larger=True)["larger"])

    def test_refit_that_fails_a_check_keeps_the_output(self):
        self.plan_and_convert(REFIT_SOURCE, reencode_video=True)
        output_hash = sha256_of(self.root / REFIT_OUTPUT)
        with unittest.mock.patch("vidfunnel.encode.refit_checks", return_value="identity tag: made up for the test"):
            summary, text = self.refit()
        self.assertEqual(summary.counts["kept"], 1, text)
        self.assertEqual(sha256_of(self.root / REFIT_OUTPUT), output_hash)
        self.assertEqual(self.leftover_parts(), [])
        saved = load_record(self.root / REFIT_SOURCE)
        self.assertEqual(saved["stages"]["refit"]["reason"], "identity tag: made up for the test")
        self.assertIn("fit-capped", saved["flags"])

    def test_dry_run_prints_the_refit_command_and_writes_nothing(self):
        self.plan_and_convert(REFIT_SOURCE, reencode_video=True)
        before = listing(self.root)
        record_bytes = record_path(self.root / REFIT_SOURCE).read_bytes()
        summary, text = self.refit(dry_run=True)
        self.assertIn("-crf 19", text)
        self.assertIn("-vf hqdn3d=2:1:2:3,setparams=", text)
        self.assertIn("would refit '{}'".format(REFIT_SOURCE), text)
        self.assertEqual(listing(self.root), before)
        self.assertEqual(record_path(self.root / REFIT_SOURCE).read_bytes(), record_bytes)

    def test_without_refit_a_larger_output_is_left_alone(self):
        self.plan_and_convert(REFIT_SOURCE, reencode_video=True)
        record_bytes = record_path(self.root / REFIT_SOURCE).read_bytes()
        output_hash = sha256_of(self.root / REFIT_OUTPUT)
        progress = quiet()
        summary = convert_paths(TOOLS, [str(self.root)], convert_options(), progress)
        self.assertEqual(summary.counts["skipped"], 1, progress.stream.getvalue())
        self.assertEqual(record_path(self.root / REFIT_SOURCE).read_bytes(), record_bytes)
        self.assertEqual(sha256_of(self.root / REFIT_OUTPUT), output_hash)

    def test_changed_output_is_skipped_with_its_reason(self):
        self.plan_and_convert(REFIT_SOURCE, reencode_video=True)
        with open(str(self.root / REFIT_OUTPUT), "ab") as handle:
            handle.write(b"x")
        record_bytes = record_path(self.root / REFIT_SOURCE).read_bytes()
        record = load_record(self.root / REFIT_SOURCE)
        outcome, reason = refit_one(TOOLS, self.root / REFIT_SOURCE, record, convert_options(), quiet())
        self.assertEqual(outcome, "skipped")
        self.assertIn("changed since convert", reason)
        self.assertEqual(record_path(self.root / REFIT_SOURCE).read_bytes(), record_bytes)
        (self.root / REFIT_OUTPUT).unlink()
        outcome, reason = refit_one(TOOLS, self.root / REFIT_SOURCE, record, convert_options(), quiet())
        self.assertEqual((outcome, reason), ("skipped", "output '{}' is missing".format(REFIT_OUTPUT)))

    def run_with_changed_argv(self, change):
        """A --refit run whose ffmpeg argv goes through change(argv) first; the real ffmpeg then runs it."""
        def changed_run(argv, *rest, **values):
            return ffmpeg_run(change(list(argv)), *rest, **values)
        with unittest.mock.patch("vidfunnel.encode.run_tool", side_effect=changed_run):
            return self.refit()

    def assert_kept_with_reason(self, summary, text, output_hash, reason_start):
        self.assertEqual(summary.counts["kept"], 1, text)
        self.assertEqual(summary.counts["refitted"], 0, text)
        self.assertEqual(sha256_of(self.root / REFIT_OUTPUT), output_hash)
        self.assertEqual(self.leftover_parts(), [])
        saved = load_record(self.root / REFIT_SOURCE)
        stage = saved["stages"]["refit"]
        self.assertEqual(stage["outcome"], "kept-existing")
        self.assertTrue(stage["reason"].startswith(reason_start), stage["reason"])
        self.assertIn("fit-capped", saved["flags"])
        self.assertNotIn("refitted", saved["flags"])
        return saved

    def test_refit_output_without_the_identity_tag_is_kept_existing(self):
        self.plan_and_convert(REFIT_SOURCE, reencode_video=True)
        output_hash = sha256_of(self.root / REFIT_OUTPUT)

        def without_tag(argv):
            at = argv.index("-metadata")
            self.assertTrue(argv[at + 1].startswith("vidfunnel="), argv)
            return argv[:at] + argv[at + 2:]
        summary, text = self.run_with_changed_argv(without_tag)
        saved = self.assert_kept_with_reason(summary, text, output_hash, "identity tag: ")
        self.assertIn("no readable 'vidfunnel' tag", saved["stages"]["refit"]["reason"])

    def test_refit_output_without_its_audio_stream_is_kept_existing(self):
        self.plan_and_convert(REFIT_SOURCE, reencode_video=True)
        output_hash = sha256_of(self.root / REFIT_OUTPUT)

        def without_audio(argv):
            at = argv.index("-f")
            return argv[:at] + ["-an"] + argv[at:]
        summary, text = self.run_with_changed_argv(without_audio)
        saved = self.assert_kept_with_reason(summary, text, output_hash, "streams ")
        self.assertIn("'audio': 0", saved["stages"]["refit"]["reason"])

    def test_duration_no_worse_than_the_previous_verify_passes(self):
        # Constructed: the source's measured video duration is moved 0.3 s plus one tolerance
        # earlier, so the existing output (read here as the refit output) misses it by more than
        # the tolerance. With the previous verify's delta set to that same miss the check passes;
        # with the verify's real, smaller delta it refuses.
        from vidfunnel.encode import refit_checks
        record = self.plan_and_convert(REFIT_SOURCE, reencode_video=True)[REFIT_SOURCE]
        earlier = record["stages"]["verify"]["duration"]
        self.assertTrue(earlier["ok"], earlier)
        shift = 0.3 + earlier["tolerance"]
        video = record["stages"]["analyze"]["sync"]["durations"]["video"]
        video["duration"] = earlier["source"] - shift
        output = self.root / REFIT_OUTPUT
        refused = refit_checks(TOOLS, record, output)
        self.assertIsNotNone(refused)
        self.assertTrue(refused.startswith("video duration "), refused)
        earlier["delta"] = round(earlier["delta"] + shift, 6)
        earlier["ok"] = False
        self.assertIsNone(refit_checks(TOOLS, record, output))
        earlier["delta"] = round(earlier["delta"] - 0.01, 6)
        self.assertIsNotNone(refit_checks(TOOLS, record, output), "a miss worse than the last verify's is refused")

    def test_refit_not_larger_than_the_source_drops_the_flag_and_retire_accepts_it(self):
        # Constructed: the planned CRF is raised to 22, so the refit runs at the SD cap 23 and comes
        # out smaller than the source.
        record = self.plan_and_convert(REFIT_SOURCE, reencode_video=True)[REFIT_SOURCE]
        argv = record["stages"]["plan"]["argv"]
        argv[argv.index("-crf") + 1] = "22"
        save_record(self.root / REFIT_SOURCE, record)
        source_size = (self.root / REFIT_SOURCE).stat().st_size
        summary, text = self.refit()
        self.assertEqual(summary.counts["refitted"], 1, text)
        saved = load_record(self.root / REFIT_SOURCE)
        self.assertEqual(saved["stages"]["refit"]["attempt"]["crf"], 23)
        self.assertLessEqual((self.root / REFIT_OUTPUT).stat().st_size, source_size,
                             "the CRF 23 refit no longer comes out smaller than the source; pick another CRF")
        self.assertIn("refitted", saved["flags"])
        self.assertNotIn("output-larger", saved["flags"])
        self.assertEqual(saved["status"], "verified", text)
        from vidfunnel.retire import check_eligible
        self.assertFalse(check_eligible(self.root / REFIT_SOURCE, saved, quiet=True)["larger"])

    def test_failed_attempt_moves_to_refit_history(self):
        self.plan_and_convert(REFIT_SOURCE, reencode_video=True)
        with unittest.mock.patch("vidfunnel.encode.run_tool",
                                 side_effect=FfmpegError("made-up ffmpeg failure", stderr="made-up stderr")):
            summary, text = self.refit()
        self.assertEqual(summary.counts["failed"], 1, text)
        summary, text = self.refit()
        self.assertEqual(summary.counts["refitted"], 1, text)
        saved = load_record(self.root / REFIT_SOURCE)
        self.assertEqual(saved["stages"]["refit"]["outcome"], "replaced")
        history = saved["stages"]["refit_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["outcome"], "failed")
        self.assertEqual(history[0]["error"], "made-up ffmpeg failure")

    def test_replaced_output_carries_the_source_mtime(self):
        self.plan_and_convert(REFIT_SOURCE, reencode_video=True)
        summary, text = self.refit()
        self.assertEqual(summary.counts["refitted"], 1, text)
        self.assertEqual((self.root / REFIT_OUTPUT).stat().st_mtime_ns, (self.root / REFIT_SOURCE).stat().st_mtime_ns)
        self.assertNotIn("mtime-not-set", load_record(self.root / REFIT_SOURCE)["flags"])

    def test_replaced_output_fingerprint_is_recorded(self):
        # verify writes output.fingerprint again when it passes; it is left out here so the record shows
        # what the refit itself saved (what an interrupt or a failed verify leaves behind).
        before = self.plan_and_convert(REFIT_SOURCE, reencode_video=True)[REFIT_SOURCE]["output"]["fingerprint"]
        with unittest.mock.patch("vidfunnel.encode._verify", side_effect=lambda tools, key, record, *rest: record):
            summary, text = self.refit()
        self.assertEqual(summary.counts["refitted"], 1, text)
        saved = load_record(self.root / REFIT_SOURCE)
        self.assertEqual(saved["output"]["fingerprint"], cheap_fingerprint(self.root / REFIT_OUTPUT))
        self.assertNotEqual(saved["output"]["fingerprint"], before)

    def test_denoise_goes_after_the_records_cadence_filter_in_the_plan(self):
        # Constructed: the captured analysis of an interlaced MPEG-2 fixture gives the cadence result;
        # its filter goes at the start of the planned chain, as plan puts it there.
        from vidfunnel.encode import refit_plan
        with open(os.path.join(REPO_ROOT, "tests", "analysis_json", "mpeg2_ps_interlaced_tff_ac3.mpg.json")) as handle:
            cadence = json.load(handle)["cadence"]["result"]
        cadence_filter = cadence["filter"]
        self.assertTrue(cadence_filter.startswith("bwdif="), cadence_filter)
        record = self.plan_and_convert(REFIT_SOURCE, reencode_video=True)[REFIT_SOURCE]
        record["stages"]["analyze"]["cadence"]["result"] = cadence
        argv = record["stages"]["plan"]["argv"]
        chain = argv[argv.index("-vf") + 1]
        argv[argv.index("-vf") + 1] = cadence_filter + "," + chain
        plan, reason = refit_plan(record, self.root)
        self.assertIsNone(reason)
        self.assertTrue(plan["denoise"], plan["denoise_reason"])
        expected = cadence_filter + "," + REFIT_DENOISE + "," + chain
        self.assertEqual(plan["filters"], expected)
        self.assertEqual(plan["argv"][plan["argv"].index("-vf") + 1], expected)

    def test_plan_writing_another_output_is_skipped_with_its_reason(self):
        record = self.plan_and_convert(REFIT_SOURCE, reencode_video=True)[REFIT_SOURCE]
        elsewhere = str(self.root / "somewhere else.mp4")
        record["stages"]["plan"]["argv"][-1] = elsewhere
        save_record(self.root / REFIT_SOURCE, record)
        record_bytes = record_path(self.root / REFIT_SOURCE).read_bytes()
        output_hash = sha256_of(self.root / REFIT_OUTPUT)
        outcome, reason = refit_one(TOOLS, self.root / REFIT_SOURCE, record, convert_options(), quiet())
        self.assertEqual((outcome, reason), ("skipped", "the planned command writes '{}', not '{}'".format(
            elsewhere, self.root / REFIT_OUTPUT)))
        self.assertEqual(record_path(self.root / REFIT_SOURCE).read_bytes(), record_bytes)
        self.assertEqual(sha256_of(self.root / REFIT_OUTPUT), output_hash)
        self.assertFalse(os.path.exists(elsewhere))

    def test_output_with_the_same_size_and_other_bytes_is_skipped(self):
        self.plan_and_convert(REFIT_SOURCE, reencode_video=True)
        output = self.root / REFIT_OUTPUT
        status = output.stat()
        content = bytearray(output.read_bytes())
        middle = len(content) // 2
        content[middle] ^= 0xFF
        output.write_bytes(bytes(content))
        os.utime(str(output), ns=(status.st_atime_ns, status.st_mtime_ns))
        self.assertEqual(output.stat().st_size, status.st_size)
        self.assertEqual(output.stat().st_mtime_ns, status.st_mtime_ns)
        record = load_record(self.root / REFIT_SOURCE)
        record_bytes = record_path(self.root / REFIT_SOURCE).read_bytes()
        outcome, reason = refit_one(TOOLS, self.root / REFIT_SOURCE, record, convert_options(), quiet())
        self.assertEqual((outcome, reason),
                         ("skipped", "output '{}' no longer matches its recorded fingerprint".format(REFIT_OUTPUT)))
        self.assertEqual(record_path(self.root / REFIT_SOURCE).read_bytes(), record_bytes)

    def test_source_changed_after_plan_is_skipped(self):
        self.plan_and_convert(REFIT_SOURCE, reencode_video=True)
        source = self.root / REFIT_SOURCE
        with open(str(source), "ab") as handle:
            handle.write(b"x")
        os.utime(str(source))
        record = load_record(source)
        record_bytes = record_path(source).read_bytes()
        output_hash = sha256_of(self.root / REFIT_OUTPUT)
        outcome, reason = refit_one(TOOLS, source, record, convert_options(), quiet())
        self.assertEqual((outcome, reason), ("skipped", "source changed since plan: '{}' no longer matches the "
                                                        "fingerprint taken at plan".format(REFIT_SOURCE)))
        self.assertEqual(record_path(source).read_bytes(), record_bytes)
        self.assertEqual(sha256_of(self.root / REFIT_OUTPUT), output_hash)

    def test_failed_ffmpeg_run_is_recorded_and_tried_again(self):
        self.plan_and_convert(REFIT_SOURCE, reencode_video=True)
        output_hash = sha256_of(self.root / REFIT_OUTPUT)
        with unittest.mock.patch("vidfunnel.encode.run_tool",
                                 side_effect=FfmpegError("made-up ffmpeg failure", stderr="made-up stderr")):
            summary, text = self.refit()
        self.assertEqual(summary.counts["failed"], 1, text)
        self.assertEqual(summary.counts["refitted"] + summary.counts["kept"], 0, text)
        self.assertEqual(sha256_of(self.root / REFIT_OUTPUT), output_hash)
        self.assertEqual(self.leftover_parts(), [])
        saved = load_record(self.root / REFIT_SOURCE)
        stage = saved["stages"]["refit"]
        self.assertEqual(stage["outcome"], "failed")
        self.assertEqual(stage["error"], "made-up ffmpeg failure")
        self.assertNotIn("fit-capped", saved["flags"])
        self.assertIn("output-larger", saved["flags"])
        summary, text = self.refit(dry_run=True)
        self.assertIn("would refit '{}'".format(REFIT_SOURCE), text)
        self.assertEqual(summary.skip_reasons, {}, text)

    def test_refit_option_parses(self):
        from vidfunnel import cli
        args = cli.build_parser().parse_args(["convert", "--refit", "somewhere"])
        self.assertTrue(args.refit)
        self.assertFalse(cli.build_parser().parse_args(["convert"]).refit)


@unittest.skipIf(shutil.which("par2") is None, "par2 not on PATH; write_par2 test skipped")
class WriteParityTest(unittest.TestCase):

    def test_write_par2_files_exist_and_verify(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "clip with space.mp4"
            output.write_bytes(os.urandom(300000))
            files = write_par2(output, 10, progress=quiet())
            self.assertTrue(files)
            for path in files:
                self.assertTrue(path.is_file(), path)
            self.assertIn(Path(folder) / "clip with space.mp4.par2", files)
            check = subprocess.run(["par2", "verify", "-q", str(Path(folder) / "clip with space.mp4.par2")],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                                   cwd=folder)
            self.assertEqual(check.returncode, 0, check.stdout)


class Par2StandInTest(unittest.TestCase):
    """write_par2 with a stand-in par2 on PATH: what the child inherits and when it is not started."""

    def setUp(self):
        import vidfunnel.stopfile
        from vidfunnel.interrupt import interrupted
        self.addCleanup(interrupted.clear)
        self.addCleanup(vidfunnel.stopfile.reset)
        holder = tempfile.TemporaryDirectory(prefix="vidfunnel-par2-test-")
        self.addCleanup(holder.cleanup)
        self.folder = Path(holder.name) / "library"
        self.folder.mkdir()
        self.marker = Path(holder.name) / "par2-started"
        fake_bin = Path(holder.name) / "bin"
        fake_bin.mkdir()
        stand_in = fake_bin / "par2"
        # Fails unless the descriptor named in HELD_DESCRIPTOR is open, then writes the index file par2 would.
        stand_in.write_text("#!{}\nimport os, sys\nopen({!r}, 'w').close()\n"
                            "os.fstat(int(os.environ['HELD_DESCRIPTOR']))\n"
                            "open(sys.argv[sys.argv.index('--') + 1], 'w').close()\n".format(
                                sys.executable, str(self.marker)), encoding="utf-8")
        os.chmod(str(stand_in), 0o755)
        path = unittest.mock.patch.dict(os.environ, {"PATH": "{}:{}".format(fake_bin, os.environ.get("PATH", ""))})
        path.start()
        self.addCleanup(path.stop)
        self.output = self.folder / "harbour.mp4"
        self.output.write_bytes(b"converted output " * 256)

    def test_par2_inherits_the_folder_lock_descriptor(self):
        from vidfunnel.record import FolderLock
        with FolderLock(self.folder) as lock:
            with unittest.mock.patch.dict(os.environ, {"HELD_DESCRIPTOR": str(lock.handle)}):
                files = write_par2(self.output, 5, progress=quiet())
        self.assertTrue(self.marker.exists())
        self.assertEqual([path.name for path in files], ["harbour.mp4.par2"])

    def test_par2_is_not_started_under_a_now_stop_file(self):
        import vidfunnel.stopfile
        from vidfunnel.record import _write_json_atomic
        _write_json_atomic(vidfunnel.stopfile.stop_path(str(self.folder)),
                           {"host": "elsewhere", "requested": None, "mode": "now"})
        vidfunnel.stopfile._current.folder = str(self.folder)  # this thread's item, as check_item sets it
        with unittest.mock.patch.dict(os.environ, {"HELD_DESCRIPTOR": "0"}), \
                unittest.mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                write_par2(self.output, 5, progress=quiet())
        self.assertFalse(self.marker.exists(), "par2 was started under a 'now' stop file")


class LineStreamPipeTest(unittest.TestCase):
    def test_a_tagged_line_stream_over_a_closed_pipe_ends_quietly(self):
        """The reader of stdout is gone: the lines are dropped and the run exits 0, without Python's
        'Exception ignored on flushing sys.stdout' and exit status 120 (needs _LineStream.fileno)."""
        script = ("import sys\n"
                  "from vidfunnel.encode import _LineStream\n"
                  "from vidfunnel.progress import say, tagged\n"
                  "sys.stdin.readline()\n"
                  "stream = tagged(_LineStream(sys.stdout), '[convert 1/2]')\n"
                  "for number in range(3):\n"
                  "    say('line {}'.format(number), stream)\n"
                  "print('after the lines')\n")
        process = subprocess.Popen([sys.executable, "-c", script], cwd=REPO_ROOT,
                                   env=dict(os.environ, PYTHONPATH=REPO_ROOT), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        process.stdout.close()  # the reader is gone before the first line is written
        _out, errors = process.communicate("go\n", timeout=30)
        self.assertNotIn("BrokenPipeError", errors)
        self.assertEqual(process.returncode, 0, errors)


if __name__ == "__main__":
    unittest.main()

# End of File #
