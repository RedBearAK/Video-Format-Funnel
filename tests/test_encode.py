"""Tests for vidfunnel.encode: temp names, skip rules, and convert runs on planned fixture copies.

The pure tests need no ffmpeg. The integration tests copy fixtures from
tests/fixtures into a temporary folder, plan them with vidfunnel.plan, and
convert the copies (never the fixture folder itself); they skip with a
message when ffmpeg or the fixtures are missing. Whether a converted record
ends at 'converted' or 'verified' depends on whether vidfunnel.verify is
present, so both are accepted.
"""

VERSION = "20261102"

import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

from vidfunnel.encode import (
    convert_one,
    convert_paths,
    expected_duration,
    output_path_for,
    part_path,
    record_key,
    run,
    skip_reason,
    write_par2,
)
from vidfunnel.ffmpeg import ToolNotFound, find_tools
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
    defaults = dict(par2=None, verbose=False, jobs=1, dry_run=False)
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


if __name__ == "__main__":
    unittest.main()

# End of File #
