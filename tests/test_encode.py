"""Tests for vidfunnel.encode: temp names, skip rules, and convert runs on planned fixture copies.

The pure tests need no ffmpeg. The integration tests copy fixtures from
tests/fixtures into a temporary folder, plan them with vidfunnel.plan, and
convert the copies (never the fixture folder itself); they skip with a
message when ffmpeg or the fixtures are missing. Whether a converted record
ends at 'converted' or 'verified' depends on whether vidfunnel.verify is
present, so both are accepted.
"""

VERSION = "20261109.1"

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

import vidfunnel
from vidfunnel.encode import (
    REFIT_DENOISE,
    argv_needs,
    capability_problem,
    convert_one,
    convert_paths,
    expected_duration,
    output_path_for,
    parse_crf,
    part_path,
    record_key,
    refit_argv,
    refit_base_crf,
    refit_crf,
    refit_denoise,
    refit_one,
    refit_selection,
    run,
    skip_reason,
    write_par2,
)
from vidfunnel.ffmpeg import FfmpegError, ToolNotFound, current_identity, find_tools
from vidfunnel.ffmpeg import run as ffmpeg_run
from vidfunnel.plan import make_options, plan_paths, record_value_problem, render_record
from vidfunnel.record import cheap_fingerprint, load_record, new_record, record_path, save_record, set_status

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


# --- rendered at convert time: records built from the captured probe and analysis -----------

CAPTURED_PROBES = os.path.join(REPO_ROOT, "tests", "probe_json")
CAPTURED_ANALYSES = os.path.join(REPO_ROOT, "tests", "analysis_json")
STAND_IN_FFMPEG_VERSION = "7.1.5-stand-in"
FULL_LISTING = {
    "version": STAND_IN_FFMPEG_VERSION, "configuration": ["--enable-gpl"],
    "encoders": {"libx264", "aac", "mov_text", "srt"}, "decoders": set(), "muxers": {"mp4", "srt", "sup", "matroska"},
    "demuxers": {"dvdvideo", "mov", "avi"}, "bsfs": {"h264_metadata"},
    "filters": {"setparams", "setsar", "fieldmatch", "decimate", "bwdif", "scale", "hqdn3d", "aresample", "atempo"},
}


def listing_without(kind, name):
    """FULL_LISTING with one name taken out of one of its sets."""
    capabilities = {key: (set(value) if isinstance(value, set) else value) for key, value in FULL_LISTING.items()}
    capabilities[kind].discard(name)
    return capabilities


def captured(fixture):
    """(probe, analysis) captured for a fixture under tests/probe_json and tests/analysis_json."""
    with open(os.path.join(CAPTURED_PROBES, fixture + ".json")) as handle:
        probe = json.load(handle)
    with open(os.path.join(CAPTURED_ANALYSES, fixture + ".json")) as handle:
        analysis = json.load(handle)
    probe.pop("raw", None)
    return probe, analysis


def write_bytes(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def captured_record(folder, fixture, action, kind="file", stand_in=None):
    """A planned record in `folder` built from a fixture's captured probe and analysis, its key and source files.

    The source files are a few bytes of stand-in content (nothing here
    decodes them). kind 'file' names the source after the fixture;
    'vob-group' is VTS_01_1.VOB and VTS_01_2.VOB; 'dvd-title' is title 1 of
    the folder 'Holiday disc'. stages.plan.argv holds a stored command that
    names `stand_in` (a program that must never run) and an output elsewhere.
    Returns (key, record) without saving it.
    """
    folder = Path(folder)
    probe, analysis = captured(fixture)
    if kind == "vob-group":
        members = [write_bytes(folder / name, name.encode() * 64) for name in ("VTS_01_1.VOB", "VTS_01_2.VOB")]
        record = new_record(members[0], folder)
        record["source"].update(kind="vob-group", members=[{"name": path.name, "fingerprint": cheap_fingerprint(path)}
                                                           for path in members])
        digest = hashlib.sha256(b"".join(path.read_bytes() for path in members)).hexdigest()
        output_name = "VTS_01.mp4"
    elif kind == "dvd-title":
        index = write_bytes(folder / "Holiday disc" / "VIDEO_TS.IFO", b"DVDVIDEO-VMG" * 64)
        record = new_record(index, folder)
        record["source"].update(name="Holiday disc", kind="dvd-title", dvd_title=1, fingerprint_of=index.name,
                                size=None)
        digest = sha256_of(index)
        output_name = "Holiday disc_title01.mp4"
    else:
        source = write_bytes(folder / fixture, b"stand-in source bytes " * 64)
        record = new_record(source, folder)
        record["source"]["kind"] = "file"
        digest = sha256_of(source)
        output_name = Path(fixture).stem + ".mp4"
    record["id"] = FIRST_RECORD_ID
    record["source"]["sha256"] = digest
    record["action"] = action
    record["output"]["name"] = output_name
    stored = [str(stand_in or "/nonexistent/stored-ffmpeg"), "-i", "/elsewhere/source", "-f", "mp4",
              "/elsewhere/{}".format(output_name)]
    record["stages"] = {"probe": probe, "analyze": analysis,
                        "plan": {"plain_mp4": False, "mark_outputs": False, "par2": None, "sidecars_skipped": [],
                                 "argv": stored, "sidecars": [], "action": action}}
    tools = types.SimpleNamespace(ffmpeg="/usr/bin/ffmpeg", ffprobe="/usr/bin/ffprobe")
    record["output"]["sidecars"] = None
    record["output"]["sidecars"] = [Path(entry["path"]).name
                                    for entry in render_record(tools, folder, record)["sidecars"]]
    set_status(record, "planned")
    return record_key(folder, record), record


def stand_in_ffmpeg(path, calls):
    """An executable at `path` that prints an ffmpeg -version banner, logging each start to `calls`.

    Asked for anything but -version it logs its last argument and exits 1
    with a made-up error, so a conversion through it fails.
    """
    path.write_text("#!/bin/sh\n"
                    "case \"$*\" in *-version*) echo version >> '{calls}'\n"
                    "  echo 'ffmpeg version {version} Copyright (c) 2000-2026 the FFmpeg developers'\n"
                    "  echo 'configuration: --enable-gpl'; exit 0;; esac\n"
                    "for last in \"$@\"; do :; done\n"
                    "echo \"run $last\" >> '{calls}'\n"
                    "echo 'made-up stand-in failure' >&2\n"
                    "exit 1\n".format(calls=calls, version=STAND_IN_FFMPEG_VERSION))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class ShapeSetter(object):
    """Sets one value deep in a record: path is a list of keys and list positions from the record's root."""

    def __init__(self, path, value):
        self.path = path
        self.value = value

    def field(self):
        text = ""
        for part in self.path:
            text += "[{}]".format(part) if isinstance(part, int) else ("." if text else "") + part
        return text

    def apply(self, record):
        target = record
        for part in self.path[:-1]:
            target = target[part]
        target[self.path[-1]] = self.value


# Each entry: (source kind, path, value). One record value of a form or type analyze and plan never write.
CADENCE = ["stages", "analyze", "cadence", "result"]
PIXELART = ["stages", "analyze", "pixelart", "result"]
ASPECT = ["stages", "analyze", "aspect", "result"]
COLOUR = ["stages", "analyze", "colour", "result"]
FIRST_AUDIO = ["stages", "analyze", "audio", 0, "result"]
FIRST_SYNC = ["stages", "analyze", "sync", "result", 0]
REJECTED_SHAPES = [
    ("file", ["source", "name"], "../xvid_avi_mp3.avi"),
    ("file", ["source", "name"], "clips/xvid_avi_mp3.avi"),
    ("file", ["source", "name"], ""),
    ("file", ["source", "name"], "."),
    ("file", ["source", "name"], ".."),
    ("file", ["source", "name"], "xvid\0avi_mp3.avi"),
    ("file", ["source", "name"], ["xvid_avi_mp3.avi"]),
    ("file", ["source", "name"], None),
    ("file", ["source", "kind"], "folder"),
    ("vob-group", ["source", "members", 0, "name"], "../VTS_01_1.VOB"),
    ("vob-group", ["source", "members", 1, "name"], "elsewhere/VTS_01_2.VOB"),
    ("vob-group", ["source", "members", 1, "name"], 2),
    ("vob-group", ["source", "members"], []),
    ("vob-group", ["source", "members"], "VTS_01_1.VOB"),
    ("dvd-title", ["source", "dvd_title"], "1"),
    ("dvd-title", ["source", "dvd_title"], 0),
    ("dvd-title", ["source", "dvd_title"], 100),
    ("dvd-title", ["source", "dvd_title"], True),
    ("dvd-title", ["source", "dvd_title"], 1.5),
    ("dvd-title", ["source", "fingerprint_of"], "../VIDEO_TS.IFO"),
    ("file", ["output", "name"], "../xvid_avi_mp3.mp4"),
    ("file", ["output", "name"], "/tmp/xvid_avi_mp3.mp4"),
    ("file", ["output", "name"], "xvid_avi_mp3.mkv"),
    ("file", ["output", "name"], ".mp4"),
    ("file", ["output", "name"], 7),
    ("file", ["output", "interim_name"], "elsewhere/xvid_avi_mp3.vidfunnel.mp4"),
    ("file", ["output", "interim_name"], ["xvid_avi_mp3.vidfunnel.mp4"]),
    ("file", ["output", "sidecars"], ["../xvid_avi_mp3.srt"]),
    ("file", ["output", "sidecars"], "xvid_avi_mp3.srt"),
    ("file", ["output", "interim_sidecars"], [".."]),
    ("file", ["stages", "plan", "sidecars_skipped"], [3]),
    ("file", ["stages", "plan", "sidecars_skipped"], ["subs/xvid_avi_mp3.srt"]),
    ("file", ["id"], "not-a-record-id"),
    ("file", ["id"], 12345),
    ("file", ["id"], FIRST_RECORD_ID.upper()),
    ("file", ["source", "sha256"], "abc"),
    ("file", ["source", "sha256"], None),
    ("file", ["source", "sha256"], ["0" * 64]),
    ("file", ["stages", "plan"], "planned"),
    ("file", ["stages", "plan", "plain_mp4"], "yes"),
    ("file", ["stages", "plan", "plain_mp4"], None),
    ("file", ["stages", "plan", "mark_outputs"], "no"),
    ("file", ["stages", "plan", "par2"], "ten"),
    ("file", ["stages", "plan", "par2"], 0),
    ("file", ["stages", "plan", "par2"], True),
    ("file", ["stages", "probe", "video", "index"], "0"),
    ("file", ["stages", "probe", "video", "index"], -1),
    ("file", ["stages", "probe", "video", "index"], None),
    ("file", ["stages", "probe", "video", "height"], "480"),
    ("file", ["stages", "probe", "video", "height"], 0),
    ("file", ["stages", "probe", "video", "fps"], "25"),
    ("file", ["stages", "probe", "video", "fps"], float("inf")),
    ("file", ["stages", "probe", "video", "fps"], True),
    ("file", ["stages", "probe", "video", "avg_fps"], -25.0),
    ("file", ["stages", "probe", "streams"], "video,audio"),
    ("file", ["stages", "probe", "streams"], ["video"]),
    ("file", CADENCE + ["filter"], "movie=/etc/passwd"),
    ("file", CADENCE + ["filter"], "bwdif=mode=1:parity=0,movie=/etc/passwd"),
    ("file", CADENCE + ["filter"], "fieldmatch,decimate,amovie=/etc/passwd"),
    ("file", CADENCE + ["filter"], "fieldmatch,decimate[out];movie=/etc/passwd"),
    ("file", CADENCE + ["filter"], "yadif"),
    ("file", CADENCE + ["filter"], "bwdif=mode=1:parity=0\n"),
    ("file", CADENCE + ["filter"], ["bwdif=mode=1:parity=0"]),
    ("file", CADENCE + ["level"], "5.1"),
    ("file", CADENCE + ["level"], 4.1),
    ("file", CADENCE + ["output_fps"], "25"),
    ("file", CADENCE + ["output_fps"], "0/1"),
    ("file", CADENCE + ["output_fps"], "abc"),
    ("file", CADENCE + ["output_fps"], 25),
    ("file", CADENCE + ["fps_multiplier"], "2.0"),
    ("file", CADENCE + ["fps_multiplier"], 3.0),
    ("file", CADENCE + ["fps_multiplier"], True),
    ("file", PIXELART + ["filter"], "scale=iw*4:ih*4:flags=neighbor"),
    ("file", PIXELART + ["filter"], "scale=iw*2:ih*2:flags=neighbor,movie=/etc/passwd"),
    ("file", PIXELART + ["filter"], "scale=iw*2:ih*3:flags=neighbor"),
    ("file", PIXELART + ["tune"], "film"),
    ("file", PIXELART + ["tune"], 1),
    ("file", ASPECT + ["sar"], "8:9,movie=/etc/passwd"),
    ("file", ASPECT + ["sar"], "8/9"),
    ("file", ASPECT + ["sar"], "0:9"),
    ("file", ASPECT + ["sar"], [8, 9]),
    ("file", ASPECT + ["dar"], "4:3:movie"),
    ("file", ASPECT + ["dar"], 1.33),
    ("file", COLOUR + ["range"], "pc"),
    ("file", COLOUR + ["range"], 0),
    ("file", COLOUR + ["space"], "bt709:movie=/etc/passwd"),
    ("file", COLOUR + ["space"], "BT709"),
    ("file", COLOUR + ["space"], 1),
    ("file", COLOUR + ["primaries"], "bt709,movie=/etc/passwd"),
    ("file", COLOUR + ["trc"], "bt709=on"),
    ("file", ["stages", "analyze", "audio"], "copy"),
    ("file", ["stages", "analyze", "audio", 0], "copy"),
    ("file", ["stages", "analyze", "audio", 0, "result"], None),
    ("file", FIRST_AUDIO + ["action"], "flac"),
    ("file", FIRST_AUDIO + ["action"], None),
    ("file", FIRST_AUDIO + ["index"], "1"),
    ("file", FIRST_AUDIO + ["bitrate"], "192k,amovie=/etc/passwd"),
    ("file", FIRST_AUDIO + ["bitrate"], 192),
    ("file", FIRST_AUDIO + ["filters"], "aresample=async=1,amovie=/etc/passwd"),
    ("file", FIRST_AUDIO + ["filters"], "volume=2"),
    ("file", FIRST_AUDIO + ["filters"], ["aresample=async=1"]),
    ("file", ["stages", "analyze", "sync", "result"], "none"),
    ("file", FIRST_SYNC + ["index"], "1"),
    ("file", FIRST_SYNC + ["offset"], "0.3"),
    ("file", FIRST_SYNC + ["offset"], [0.3]),
    ("file", FIRST_SYNC + ["measured_offset"], "early"),
    ("file", ["stages", "analyze", "subtitles", "result"], "mov_text"),
    ("file", ["stages", "analyze", "subtitles", "result"], ["mov_text"]),
]


class RenderedRecordTest(unittest.TestCase):
    """convert renders from the record with this machine's ffmpeg, a stand-in here; no real ffmpeg is needed."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.calls = self.base / "calls"
        self.stand_in = stand_in_ffmpeg(self.base / "ffmpeg", self.calls)
        self.tools = types.SimpleNamespace(ffmpeg=str(self.stand_in), ffprobe=str(self.stand_in))
        self.stored = write_bytes(self.base / "stored-command", b"")
        self.stored.write_text("#!/bin/sh\necho stored >> '{}'\nexit 0\n".format(self.calls))
        self.stored.chmod(self.stored.stat().st_mode | stat.S_IXUSR)
        self.folders = 0

    def folder(self):
        self.folders += 1
        folder = self.base / "library{}".format(self.folders)
        folder.mkdir()
        return folder

    def calls_made(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def convert(self, folder, capabilities=None, **values):
        progress = quiet()
        with unittest.mock.patch("vidfunnel.encode.ffmpeg_capabilities", return_value=capabilities or FULL_LISTING):
            summary = convert_paths(self.tools, [str(folder)], convert_options(**values), progress)
        return summary, progress.stream.getvalue()

    def saved(self, fixture, action, kind="file", change=None):
        folder = self.folder()
        key, record = captured_record(folder, fixture, action, kind, self.stored)
        if change is not None:
            change.apply(record)
        save_record(key, record)
        return folder, key

    def convert_untouched(self, folder, key, capabilities, expected):
        """Convert the folder with this capability listing; the record is skipped and nothing changes."""
        before = listing(folder)
        record_bytes = record_path(key).read_bytes()
        summary, text = self.convert(folder, capabilities)
        self.assertEqual(summary.counts["skipped"], 1, text)
        self.assertEqual(summary.counts["failed"], 0, text)
        self.assertEqual(summary.counts["converted"] + summary.counts["verified"], 0, text)
        self.assertIn(expected, text)
        self.assertEqual(record_path(key).read_bytes(), record_bytes)
        self.assertEqual(listing(folder), before)
        self.assertEqual([call for call in self.calls_made() if call != "version"], [], text)

    def test_every_captured_analysis_passes_the_value_checks_and_renders(self):
        tools = types.SimpleNamespace(ffmpeg="/usr/bin/ffmpeg", ffprobe="/usr/bin/ffprobe")
        fixtures = sorted(name[:-5] for name in os.listdir(CAPTURED_ANALYSES) if name.endswith(".json"))
        self.assertGreater(len(fixtures), 20)
        for fixture in fixtures:
            for action in ("remux", "reencode"):
                with self.subTest(fixture=fixture, action=action):
                    folder = self.folder()
                    _key, record = captured_record(folder, fixture, action)
                    self.assertIsNone(record_value_problem(record))
                    argv = render_record(tools, folder, record)["argv"]
                    self.assertEqual(argv[0], tools.ffmpeg)
                    self.assertEqual(argv[-3:], ["-f", "mp4", str(folder / record["output"]["name"])])
                    self.assertFalse([item for item in argv if "movie=" in item], argv)

    def test_every_kind_of_source_passes_the_value_checks(self):
        for kind in ("file", "vob-group", "dvd-title"):
            with self.subTest(kind=kind):
                _key, record = captured_record(self.folder(), "xvid_avi_mp3.avi", "reencode", kind)
                self.assertIsNone(record_value_problem(record))

    def test_a_source_without_a_kind_is_a_file_as_before(self):
        folder = self.folder()
        _key, record = captured_record(folder, "xvid_avi_mp3.avi", "reencode")
        expected = render_record(self.tools, folder, record)["argv"]
        del record["source"]["kind"]
        self.assertIsNone(record_value_problem(record))
        self.assertEqual(render_record(self.tools, folder, record)["argv"], expected)

    def test_each_rejected_shape_is_skipped_untouched_with_the_field_named(self):
        for kind, path, value in REJECTED_SHAPES:
            change = ShapeSetter(path, value)
            with self.subTest(field=change.field(), value=value):
                folder, key = self.saved("xvid_avi_mp3.avi", "reencode", kind, change)
                before = listing(folder)
                record_bytes = record_path(key).read_bytes()
                summary, text = self.convert(folder)
                self.assertEqual(summary.counts["skipped"], 1, text)
                self.assertEqual(summary.counts["failed"], 0, text)
                self.assertRegex(text, r": {}(\[[0-9]+\])? is ".format(re.escape(change.field())))
                self.assertEqual(record_path(key).read_bytes(), record_bytes)
                self.assertEqual(listing(folder), before)
                self.assertEqual(self.calls_made(), [], text)

    def test_a_dry_run_names_the_field_too(self):
        folder, key = self.saved("xvid_avi_mp3.avi", "reencode",
                                 change=ShapeSetter(CADENCE + ["filter"], "bwdif=mode=1:parity=0,movie=/etc/passwd"))
        record_bytes = record_path(key).read_bytes()
        _summary, text = self.convert(folder, dry_run=True)
        self.assertIn("would skip 'xvid_avi_mp3.avi': stages.analyze.cadence.result.filter is "
                      "'bwdif=mode=1:parity=0,movie=/etc/passwd', not ", text)
        self.assertNotIn("-c:v", text)
        self.assertEqual(record_path(key).read_bytes(), record_bytes)

    def test_missing_libx264_skips_the_record(self):
        folder, key = self.saved("xvid_avi_mp3.avi", "reencode")
        self.convert_untouched(folder, key, listing_without("encoders", "libx264"),
                                      "ffmpeg '{}' has no encoder 'libx264'".format(self.stand_in))

    def test_missing_aac_encoder_skips_the_record(self):
        folder, key = self.saved("wmv2_asf_wmav2.wmv", "reencode")
        self.convert_untouched(folder, key, listing_without("encoders", "aac"),
                                      "ffmpeg '{}' has no encoder 'aac'".format(self.stand_in))

    def test_missing_video_filter_skips_the_record(self):
        folder, key = self.saved("mpeg2_ps_telecined_ac3.mpg", "reencode")
        self.convert_untouched(folder, key, listing_without("filters", "decimate"),
                                      "ffmpeg '{}' has no filter 'decimate'".format(self.stand_in))

    def test_missing_audio_filter_skips_the_record(self):
        folder, key = self.saved("wmv2_asf_wmav2.wmv", "remux")
        self.convert_untouched(folder, key, listing_without("filters", "aresample"),
                                      "ffmpeg '{}' has no filter 'aresample'".format(self.stand_in))

    def test_missing_h264_metadata_skips_a_remux_that_uses_it(self):
        folder, key = self.saved("letterbox_720x480_sar_1x1.mkv", "remux")
        self.convert_untouched(folder, key, listing_without("bsfs", "h264_metadata"),
                                      "ffmpeg '{}' has no bitstream filter 'h264_metadata'".format(self.stand_in))

    def test_missing_dvdvideo_skips_a_dvd_title(self):
        folder, key = self.saved("mpeg2_ps_ntsc_dar_4x3_ac3.mpg", "reencode", "dvd-title")
        self.convert_untouched(folder, key, listing_without("demuxers", "dvdvideo"),
                                      "ffmpeg '{}' has no demuxer 'dvdvideo'".format(self.stand_in))

    def test_a_listing_with_everything_lets_each_kind_through_to_ffmpeg(self):
        for fixture, action, kind in (("xvid_avi_mp3.avi", "reencode", "file"),
                                      ("letterbox_720x480_sar_1x1.mkv", "remux", "file"),
                                      ("mpeg2_ps_ntsc_dar_4x3_ac3.mpg", "reencode", "vob-group"),
                                      ("mpeg2_ps_ntsc_dar_4x3_ac3.mpg", "reencode", "dvd-title")):
            with self.subTest(fixture=fixture, kind=kind):
                folder, _key = self.saved(fixture, action, kind)
                summary, text = self.convert(folder)
                self.assertEqual(summary.counts["failed"], 1, text)
                self.assertIn("made-up stand-in failure", text)

    def test_failed_run_records_the_command_the_ffmpeg_and_the_version(self):
        folder, key = self.saved("xvid_avi_mp3.avi", "reencode")
        summary, text = self.convert(folder)
        self.assertEqual(summary.counts["failed"], 1, text)
        record = load_record(key)
        self.assertEqual(record["status"], "failed")
        stage = record["stages"]["convert"]
        self.assertEqual(stage["outcome"], "failed")
        self.assertEqual(stage["argv"][0], str(self.stand_in))
        self.assertEqual(stage["argv"][-1], str(part_path(folder / "xvid_avi_mp3.mp4", FIRST_RECORD_ID)))
        self.assertIn("-i", stage["argv"])
        self.assertEqual(stage["argv"][stage["argv"].index("-i") + 1], str(folder / "xvid_avi_mp3.avi"))
        self.assertEqual(stage["sidecar_argvs"], [])
        self.assertEqual(stage["ffmpeg"], current_identity(str(self.stand_in)))
        self.assertEqual(stage["ffmpeg"]["version"], STAND_IN_FFMPEG_VERSION)
        self.assertEqual(stage["version"], vidfunnel.VERSION)
        self.assertNotIn("stored", self.calls_made())

    def test_the_stored_command_is_never_run(self):
        folder, key = self.saved("xvid_avi_mp3.avi", "reencode")
        stored = load_record(key)["stages"]["plan"]["argv"]
        self.assertEqual(stored[0], str(self.stored))
        summary, text = self.convert(folder)
        self.assertEqual(summary.counts["failed"], 1, text)
        self.assertNotIn("stored", self.calls_made())
        self.assertIn("run {}".format(part_path(folder / "xvid_avi_mp3.mp4", FIRST_RECORD_ID)), self.calls_made())
        self.assertFalse(os.path.exists("/elsewhere"))

    def test_two_records_ask_this_ffmpeg_for_its_identity_once(self):
        folder = self.folder()
        for fixture in ("xvid_avi_mp3.avi", "h264_avi_mp3.avi"):
            key, record = captured_record(folder, fixture, "reencode", stand_in=self.stored)
            record["id"] = FIRST_RECORD_ID if fixture.startswith("xvid") else SECOND_RECORD_ID
            save_record(key, record)
        summary, text = self.convert(folder)
        self.assertEqual(summary.counts["failed"], 2, text)
        self.assertEqual(self.calls_made().count("version"), 1, self.calls_made())

    def test_an_ffmpeg_that_does_not_run_skips_the_record(self):
        folder, key = self.saved("xvid_avi_mp3.avi", "reencode")
        self.stand_in.write_text("#!/bin/sh\necho broken >&2\nexit 3\n")
        self.convert_untouched(folder, key, None,
                                      "ffmpeg '{}' does not run here".format(self.stand_in))

    def test_sidecar_names_that_differ_from_the_analysis_skip_the_record(self):
        folder, key = self.saved("h264_mkv_srt_subtitles.mkv", "remux",
                                 change=ShapeSetter(["output", "sidecars"], ["h264_mkv_srt_subtitles.2.srt"]))
        self.convert_untouched(folder, key, None,
                                      "output.sidecars lists 'h264_mkv_srt_subtitles.2.srt' but the record's analysis "
                                      "gives 'h264_mkv_srt_subtitles.srt'")

    def test_skipped_sidecars_stay_skipped_whatever_is_on_disk_now(self):
        folder, key = self.saved("h264_mkv_srt_subtitles.mkv", "remux",
                                 change=ShapeSetter(["stages", "plan", "sidecars_skipped"],
                                                    ["h264_mkv_srt_subtitles.srt"]))
        record = load_record(key)
        record["output"]["sidecars"] = []
        save_record(key, record)
        self.assertFalse((folder / "h264_mkv_srt_subtitles.srt").exists())
        self.assertEqual(render_record(self.tools, folder, record)["sidecars"], [])
        record["stages"]["plan"]["sidecars_skipped"] = []
        record["output"]["sidecars"] = ["h264_mkv_srt_subtitles.srt"]
        (folder / "h264_mkv_srt_subtitles.srt").write_text("a file that appeared after plan\n")
        rendered = render_record(self.tools, folder, record)["sidecars"]
        self.assertEqual([entry["path"] for entry in rendered], [str(folder / "h264_mkv_srt_subtitles.srt")])

    def test_argv_needs(self):
        argv = ["ffmpeg", "-f", "dvdvideo", "-title", "1", "-i", "/disc", "-map", "0:v:0",
                "-vf", "bwdif=mode=1:parity=0,setparams=range=tv", "-c:v", "libx264", "-c:a:0", "copy",
                "-c:a:1", "aac", "-filter:a:1", "aresample=async=1,atempo=1.01", "-bsf:v", "h264_metadata=x=1",
                "-c:s", "mov_text", "-f", "mp4", "/out.mp4"]
        self.assertEqual(argv_needs(argv), [("demuxers", "dvdvideo"), ("filters", "bwdif"), ("filters", "setparams"),
                                            ("encoders", "libx264"), ("encoders", "aac"), ("filters", "aresample"),
                                            ("filters", "atempo"), ("bsfs", "h264_metadata")])

    def test_capability_problem_names_every_missing_item_and_the_path(self):
        argv = ["ffmpeg", "-i", "/in", "-vf", "hqdn3d=2:1:2:3", "-c:v", "libx264", "-f", "mp4", "/out.mp4"]
        capabilities = listing_without("filters", "hqdn3d")
        capabilities["encoders"].discard("libx264")
        with unittest.mock.patch("vidfunnel.encode.ffmpeg_capabilities", return_value=capabilities):
            problem = capability_problem(self.tools, argv)
        self.assertEqual(problem, "ffmpeg '{}' has no filter 'hqdn3d' and no encoder 'libx264', which this record "
                                  "needs (see 'vidfunnel check')".format(self.stand_in))
        with unittest.mock.patch("vidfunnel.encode.ffmpeg_capabilities", return_value=FULL_LISTING):
            self.assertIsNone(capability_problem(self.tools, argv))


# --- integration --------------------------------------------------------------------------


def _tools_or_none():
    try:
        return find_tools()
    except ToolNotFound:
        return None


TOOLS = _tools_or_none()


def fast_x264():
    """vidfunnel.encode.run_tool running '-preset slower' as '-preset ultrafast', for speed only.

    For tests of the convert mechanics, not of x264: the record still holds
    the command as rendered (with 'slower').
    """
    def faster(argv, *rest, **values):
        argv = list(argv)
        if "-preset" in argv:
            argv[argv.index("-preset") + 1] = "ultrafast"
        return ffmpeg_run(argv, *rest, **values)
    return unittest.mock.patch("vidfunnel.encode.run_tool", side_effect=faster)
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
        convert = saved["stages"]["convert"]
        self.assertEqual(convert["argv"][0], TOOLS.ffmpeg)
        self.assertEqual(convert["argv"][-1], str(self.root / convert["part_path"]))
        self.assertEqual(convert["argv"][convert["argv"].index("-i") + 1], str(self.root / REMUX_SOURCE))
        self.assertEqual(convert["sidecar_argvs"], [])
        self.assertEqual(convert["ffmpeg"], current_identity(TOOLS.ffmpeg))
        self.assertEqual(convert["version"], vidfunnel.VERSION)

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
        # '-preset slower' takes long even on a small clip; fast_x264 runs 'ultrafast' so the test
        # checks the convert mechanics, not x264's speed.
        with fast_x264():
            stage = self.convert(REENCODE_SOURCE, record)
        self.assertEqual(stage["outcome"], "ok", stage)
        self.assertIn("slower", stage["argv"])
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

    def test_plan_made_with_another_ffmpeg_build_converts_with_this_one(self):
        # Before 20261109.1 convert refused this record ("plan again with --replan on this machine").
        record = self.plan(REMUX_SOURCE)[REMUX_SOURCE]
        record["stages"]["plan"]["ffmpeg"]["version"] = OTHER_BUILD_VERSION  # can match no real ffmpeg
        record["stages"]["plan"]["ffmpeg"]["configuration_sha256"] = "0" * 64
        save_record(self.root / REMUX_SOURCE, record)
        progress = quiet()
        summary = convert_paths(TOOLS, [str(self.root)], convert_options(), progress)
        text = progress.stream.getvalue()
        self.assertEqual(summary.counts["converted"] + summary.counts["verified"], 1, text)
        self.assertNotIn("--replan", text)
        saved = load_record(self.root / REMUX_SOURCE)
        self.assertEqual(saved["stages"]["convert"]["ffmpeg"], current_identity(TOOLS.ffmpeg))
        self.assertEqual(saved["stages"]["plan"]["ffmpeg"]["version"], OTHER_BUILD_VERSION)

    def test_plan_without_the_ffmpeg_field_converts_as_before(self):
        record = self.plan(REMUX_SOURCE)[REMUX_SOURCE]
        del record["stages"]["plan"]["ffmpeg"]
        save_record(self.root / REMUX_SOURCE, record)
        stage = self.convert(REMUX_SOURCE, record)
        self.assertEqual(stage["outcome"], "ok", stage)

    def test_bogus_codec_fails_with_stderr_and_no_part(self):
        # Constructed: the real ffmpeg is handed the rendered command with a codec it does not have.
        record = self.plan(REMUX_SOURCE)[REMUX_SOURCE]

        def bogus_codec(argv, *rest, **values):
            argv = list(argv)
            argv[argv.index("-c:v") + 1] = "nonexistent_codec"
            return ffmpeg_run(argv, *rest, **values)
        with unittest.mock.patch("vidfunnel.encode.run_tool", side_effect=bogus_codec):
            stage = self.convert(REMUX_SOURCE, record)
        self.assertEqual(stage["outcome"], "failed")
        saved = load_record(self.root / REMUX_SOURCE)
        self.assertEqual(saved["status"], "failed")
        convert = saved["stages"]["convert"]
        self.assertIn("nonexistent_codec", convert["stderr"])
        self.assertNotEqual(convert["returncode"], 0)
        self.assertEqual(convert["argv"][convert["argv"].index("-c:v") + 1], "copy")
        self.assertEqual(convert["ffmpeg"], current_identity(TOOLS.ffmpeg))
        self.assertEqual(convert["version"], vidfunnel.VERSION)
        self.assertEqual(convert["sidecar_argvs"], [])
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
        record["action"] = "reencode"  # the size rule applies to re-encodes; convert now renders a re-encode
        progress = quiet()
        with fast_x264():
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
        with fast_x264():
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
        with fast_x264():
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

    def test_dry_run_prints_the_command_rendered_here_not_the_stored_one(self):
        record = self.plan(REMUX_SOURCE)[REMUX_SOURCE]
        stored = record["stages"]["plan"]["argv"]
        stored[0] = "/nonexistent/stored-ffmpeg"
        stored[stored.index("-c:v") + 1] = "stored_only_codec"
        stored[-1] = "/nonexistent/elsewhere.mp4"
        save_record(self.root / REMUX_SOURCE, record)
        before = listing(self.root)
        record_bytes = record_path(self.root / REMUX_SOURCE).read_bytes()
        code, text = self.run_command(dry_run=True)
        self.assertEqual(code, 0, text)
        commands = [line for line in text.splitlines() if " -hide_banner " in line]
        self.assertEqual(len(commands), 1, text)
        self.assertTrue(commands[0].startswith(shlex.quote(TOOLS.ffmpeg) + " "), commands)
        self.assertIn("-c:v copy", commands[0])
        self.assertIn(shlex.quote(str(self.root / REMUX_SOURCE)), commands[0])
        self.assertNotIn("stored_only_codec", text)
        self.assertNotIn("/nonexistent/", text)
        self.assertEqual(listing(self.root), before)
        self.assertEqual(record_path(self.root / REMUX_SOURCE).read_bytes(), record_bytes)

    def test_stored_command_naming_another_program_and_output_is_not_run(self):
        record = self.plan(REMUX_SOURCE)[REMUX_SOURCE]
        marker = self.root.parent / "stored command ran"
        program = self.root.parent / "stored-program"
        program.write_text("#!/bin/sh\ntouch '{}'\nexit 0\n".format(marker))
        program.chmod(program.stat().st_mode | stat.S_IXUSR)
        elsewhere = self.root.parent / "elsewhere.mp4"
        stored = record["stages"]["plan"]["argv"]
        stored[0] = str(program)
        stored[-1] = str(elsewhere)
        save_record(self.root / REMUX_SOURCE, record)
        stage = self.convert(REMUX_SOURCE, load_record(self.root / REMUX_SOURCE))
        self.assertEqual(stage["outcome"], "ok", stage)
        self.assertFalse(marker.exists(), "the stored command was run")
        self.assertFalse(elsewhere.exists())
        self.assertTrue((self.root / "h264_high_mkv_aac.mp4").is_file())
        saved = load_record(self.root / REMUX_SOURCE)
        self.assertEqual(saved["stages"]["convert"]["argv"][0], TOOLS.ffmpeg)
        self.assertEqual(saved["stages"]["plan"]["argv"][0], str(program), "the stored command is left in place")

    def test_tree_moved_after_plan_converts_without_a_new_plan(self):
        self.plan(REMUX_SOURCE, MP4_SOURCE)
        planned_folder = str(self.root)
        moved = self.root.parent / "moved library"
        os.rename(str(self.root), str(moved))
        self.root = moved
        progress = quiet()
        summary = convert_paths(TOOLS, [str(moved)], convert_options(), progress)
        text = progress.stream.getvalue()
        self.assertEqual(summary.counts["converted"] + summary.counts["verified"], 2, text)
        self.assertEqual(summary.counts["skipped"] + summary.counts["failed"], 0, text)
        self.assertTrue((moved / "h264_high_mkv_aac.mp4").is_file())
        self.assertTrue((moved / "h264_mp4_aac.vidfunnel.mp4").is_file())
        self.assertFalse(os.path.exists(planned_folder))
        for name in (REMUX_SOURCE, MP4_SOURCE):
            saved = load_record(moved / name)
            argv = saved["stages"]["convert"]["argv"]
            self.assertEqual(argv[argv.index("-i") + 1], str(moved / name))
            self.assertEqual(os.path.dirname(argv[-1]), str(moved))
            self.assertTrue(saved["stages"]["plan"]["argv"][-1].startswith(planned_folder + os.sep))

    def test_plan_preview_is_the_command_convert_renders(self):
        names = [REMUX_SOURCE, MP4_SOURCE, REENCODE_SOURCE]
        if os.path.exists(os.path.join(FIXTURE_DIR, SUBTITLE_SOURCE)):
            names.append(SUBTITLE_SOURCE)
        for mark in (False, True):
            with self.subTest(mark_outputs=mark):
                for name in names:
                    shutil.copy2(os.path.join(FIXTURE_DIR, name), str(self.root / name))
                plan_paths(TOOLS, [str(self.root)], make_options(replan=True, mark_outputs=mark), progress=quiet())
                for name in names:
                    record = load_record(self.root / name)
                    self.assertEqual(record["status"], "planned", name)
                    self.assertIsNone(record_value_problem(record), name)
                    rendered = render_record(TOOLS, self.root, record)
                    self.assertEqual(rendered["argv"], record["stages"]["plan"]["argv"], name)
                    self.assertEqual(rendered["sidecars"], record["stages"]["plan"]["sidecars"], name)

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
        sidecar_argvs = record["stages"]["convert"]["sidecar_argvs"]
        self.assertEqual(len(sidecar_argvs), 1, sidecar_argvs)
        self.assertEqual(sidecar_argvs[0][0], TOOLS.ffmpeg)
        self.assertEqual(sidecar_argvs[0][-1], str(part_path(self.root / "Movie night.vidfunnel.srt", record["id"])))
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

    def test_base_crf_comes_from_the_command_convert_ran(self):
        convert_argv = ["ffmpeg", "-crf", "19", "-f", "mp4", "/library/.clip.mp4.0f2b6c1e.part"]
        record = refit_record(stages={"convert": {"argv": convert_argv}, "plan": {"argv": PLANNED_ARGV}})
        self.assertEqual(refit_base_crf(record), (19, "stages.convert.argv", None))

    def test_base_crf_of_a_record_converted_before_convert_recorded_its_command(self):
        record = refit_record(stages={"convert": {"outcome": "ok"}, "plan": {"argv": PLANNED_ARGV}})
        self.assertEqual(refit_base_crf(record), (18, "stages.plan.argv", None))

    def test_base_crf_that_cannot_be_read_is_a_reason(self):
        for stages, field in (({"convert": {"argv": "ffmpeg -crf 18"}, "plan": {"argv": PLANNED_ARGV}},
                               "stages.convert.argv"),
                              ({"convert": {"argv": ["ffmpeg", "-crf", "eighteen"]}, "plan": {"argv": PLANNED_ARGV}},
                               "stages.convert.argv"),
                              ({"convert": {"argv": ["ffmpeg", "-crf", "52", "out.mp4"]}}, "stages.convert.argv"),
                              ({"convert": {"argv": ["ffmpeg", "-crf", 18, "out.mp4"]}}, "stages.convert.argv"),
                              ({"convert": {}, "plan": {"argv": None}}, "stages.plan.argv"),
                              ({"convert": {}, "plan": "planned"}, "stages.plan.argv")):
            with self.subTest(stages=stages):
                crf, found, reason = refit_base_crf(refit_record(stages=stages))
                self.assertIsNone(crf)
                self.assertEqual(found, field)
                self.assertTrue(reason.startswith(field + " "), reason)

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

    def test_refit_of_a_plan_made_with_another_ffmpeg_build_refits_with_this_one(self):
        # Before 20261109.1 a refit refused this record ("plan again with --replan on this machine").
        records = self.plan_and_convert(REFIT_SOURCE, reencode_video=True)
        record = records[REFIT_SOURCE]
        self.assertIn("output-larger", record["flags"], "the fixture no longer re-encodes larger; pick another")
        record["stages"]["plan"]["ffmpeg"]["configuration_sha256"] = "0" * 64
        save_record(self.root / REFIT_SOURCE, record)
        with unittest.mock.patch("vidfunnel.encode._verify", side_effect=lambda tools, key, record, *rest: record):
            summary, text = self.refit()
        self.assertEqual(summary.counts["refitted"], 1, text)
        attempt = load_record(self.root / REFIT_SOURCE)["stages"]["refit"]["attempt"]
        self.assertEqual(attempt["ffmpeg"], current_identity(TOOLS.ffmpeg))
        self.assertEqual(attempt["version"], vidfunnel.VERSION)
        self.assertEqual(attempt["argv"][0], TOOLS.ffmpeg)
        self.assertEqual((attempt["base_crf"], attempt["base_crf_from"]), (18, "stages.convert.argv"))

    def assert_refit_skipped_untouched(self, expected, capabilities=None):
        record_bytes = record_path(self.root / REFIT_SOURCE).read_bytes()
        output_hash = sha256_of(self.root / REFIT_OUTPUT)
        if capabilities is None:
            summary, text = self.refit()
        else:
            with unittest.mock.patch("vidfunnel.encode.ffmpeg_capabilities", return_value=capabilities):
                summary, text = self.refit()
        self.assertEqual(summary.counts["skipped"], 1, text)
        self.assertEqual(summary.counts["refitted"] + summary.counts["kept"] + summary.counts["failed"], 0, text)
        self.assertEqual(summary.skip_reasons, {"other (reason printed above)": 1}, text)
        self.assertIn(expected, text)
        self.assertEqual(record_path(self.root / REFIT_SOURCE).read_bytes(), record_bytes)
        self.assertEqual(sha256_of(self.root / REFIT_OUTPUT), output_hash)
        self.assertEqual(self.leftover_parts(), [])

    def test_refit_needing_a_filter_this_ffmpeg_lacks_is_skipped(self):
        from vidfunnel.ffmpeg import capabilities
        record = self.plan_and_convert(REFIT_SOURCE, reencode_video=True)[REFIT_SOURCE]
        self.assertTrue(refit_denoise(record)[0], "the fixture no longer gets hqdn3d; pick another")
        listing_here = dict(capabilities(TOOLS))
        listing_here["filters"] = set(listing_here["filters"]) - {"hqdn3d"}
        self.assert_refit_skipped_untouched("ffmpeg '{}' has no filter 'hqdn3d'".format(TOOLS.ffmpeg), listing_here)

    def test_refit_of_a_record_with_a_filter_of_another_form_is_skipped(self):
        record = self.plan_and_convert(REFIT_SOURCE, reencode_video=True)[REFIT_SOURCE]
        record["stages"]["analyze"]["colour"]["result"]["space"] = "bt470bg,movie=/etc/passwd"
        save_record(self.root / REFIT_SOURCE, record)
        self.assert_refit_skipped_untouched("stages.analyze.colour.result.space is 'bt470bg,movie=/etc/passwd', not ")

    def test_record_converted_before_convert_recorded_its_command_refits_from_the_stored_crf(self):
        # Constructed: a convert stage without argv, ffmpeg and version, as convert wrote it before 20261109.1,
        # and a stored plan command with CRF 22, so the refit runs at the SD cap 23.
        record = self.plan_and_convert(REFIT_SOURCE, reencode_video=True)[REFIT_SOURCE]
        for field in ("argv", "sidecar_argvs", "ffmpeg", "version"):
            del record["stages"]["convert"][field]
        stored = record["stages"]["plan"]["argv"]
        stored[stored.index("-crf") + 1] = "22"
        save_record(self.root / REFIT_SOURCE, record)
        with unittest.mock.patch("vidfunnel.encode._verify", side_effect=lambda tools, key, record, *rest: record):
            summary, text = self.refit()
        self.assertEqual(summary.counts["refitted"], 1, text)
        attempt = load_record(self.root / REFIT_SOURCE)["stages"]["refit"]["attempt"]
        self.assertEqual((attempt["base_crf"], attempt["base_crf_from"], attempt["crf"]), (22, "stages.plan.argv", 23))
        self.assertEqual(attempt["argv"][attempt["argv"].index("-crf") + 1], "23")

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
        # Constructed: the CRF convert recorded is lowered to 10, so the refit at CRF 11 comes out larger
        # than the existing CRF 18 output.
        record = self.plan_and_convert(REFIT_SOURCE, reencode_video=True)[REFIT_SOURCE]
        argv = record["stages"]["convert"]["argv"]
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
        # Constructed: the CRF convert recorded is raised to 22, so the refit runs at the SD cap 23 and
        # comes out smaller than the source.
        record = self.plan_and_convert(REFIT_SOURCE, reencode_video=True)[REFIT_SOURCE]
        argv = record["stages"]["convert"]["argv"]
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
        # the rendered chain starts with its filter, as plan's does.
        from vidfunnel.encode import refit_plan
        with open(os.path.join(REPO_ROOT, "tests", "analysis_json", "mpeg2_ps_interlaced_tff_ac3.mpg.json")) as handle:
            cadence = json.load(handle)["cadence"]["result"]
        cadence_filter = cadence["filter"]
        self.assertTrue(cadence_filter.startswith("bwdif="), cadence_filter)
        record = self.plan_and_convert(REFIT_SOURCE, reencode_video=True)[REFIT_SOURCE]
        record["stages"]["analyze"]["cadence"]["result"] = cadence
        argv = record["stages"]["plan"]["argv"]
        chain = argv[argv.index("-vf") + 1]
        plan, reason = refit_plan(TOOLS, record, self.root)
        self.assertIsNone(reason)
        self.assertTrue(plan["denoise"], plan["denoise_reason"])
        expected = cadence_filter + "," + REFIT_DENOISE + "," + chain
        self.assertEqual(plan["filters"], expected)
        self.assertEqual(plan["argv"][plan["argv"].index("-vf") + 1], expected)

    def test_stored_commands_writing_elsewhere_do_not_steer_the_refit(self):
        # Before 20261109.1 the refit refused a stored command that wrote another file; now neither stored
        # command is run, and the refit writes the record's own output.
        record = self.plan_and_convert(REFIT_SOURCE, reencode_video=True)[REFIT_SOURCE]
        elsewhere = str(self.root / "somewhere else.mp4")
        record["stages"]["plan"]["argv"][-1] = elsewhere
        record["stages"]["plan"]["argv"][0] = "/nonexistent/stored-ffmpeg"
        record["stages"]["convert"]["argv"][-1] = elsewhere
        save_record(self.root / REFIT_SOURCE, record)
        with unittest.mock.patch("vidfunnel.encode._verify", side_effect=lambda tools, key, record, *rest: record):
            outcome, reason = refit_one(TOOLS, self.root / REFIT_SOURCE, record, convert_options(), quiet())
        self.assertEqual((outcome, reason), ("refitted", None))
        self.assertFalse(os.path.exists(elsewhere))
        attempt = load_record(self.root / REFIT_SOURCE)["stages"]["refit"]["attempt"]
        self.assertEqual(attempt["argv"][0], TOOLS.ffmpeg)
        self.assertEqual(attempt["argv"][-1], str(part_path(self.root / REFIT_OUTPUT, record["id"])))

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


FIRST_NAME = "first_harbour.mkv"     # a copy of SUBTITLE_SOURCE: one subtitle sidecar after its encode
SECOND_NAME = "second_meadow.mkv"    # a copy of REMUX_SOURCE
FIRST_REFIT = "first_harbour.avi"    # copies of REFIT_SOURCE, which re-encodes larger than its source
SECOND_REFIT = "second_meadow.avi"


@unittest.skipIf(TOOLS is None or shutil.which("par2") is None,
                 "ffmpeg or par2 not found on PATH; stop-file-after-encode tests skipped")
class NowStopFileAfterEncodeTest(unittest.TestCase):
    """A 'stop-all --now' file written once an item's main encode has returned: the sidecars, PAR2 and the
    record of that item are kept, and the run stops before the next item."""

    def setUp(self):
        import vidfunnel.interrupt
        import vidfunnel.stopfile
        missing = [name for name in (SUBTITLE_SOURCE, REMUX_SOURCE, REFIT_SOURCE)
                   if not os.path.exists(os.path.join(FIXTURE_DIR, name))]
        if missing:
            self.skipTest("fixtures missing ({}); run tests/make_fixtures.sh".format(", ".join(missing)))
        for flag in (vidfunnel.interrupt.interrupted, vidfunnel.interrupt.draining):
            flag.clear()
            self.addCleanup(flag.clear)
        vidfunnel.stopfile.reset()
        self.addCleanup(vidfunnel.stopfile.reset)
        holder = tempfile.TemporaryDirectory(prefix="vidfunnel-now-after-encode-")
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name) / "library"
        self.root.mkdir()

    def copy(self, fixture, name):
        shutil.copy2(os.path.join(FIXTURE_DIR, fixture), str(self.root / name))

    def write_now_file(self):
        from vidfunnel.record import _write_json_atomic
        import vidfunnel.stopfile
        _write_json_atomic(vidfunnel.stopfile.stop_path(str(self.root)),
                           {"host": "elsewhere", "requested": None, "mode": "now"})

    def main_with_now_file_after_first_encode(self, argv):
        """cli.main(argv) with the now-file written as the first encode (vidfunnel.encode.run_tool) returns."""
        from vidfunnel import cli
        calls = []

        def run_tool(*args, **kwargs):
            result = ffmpeg_run(*args, **kwargs)
            calls.append(list(args[0]))
            if len(calls) == 1:
                self.write_now_file()
            return result

        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch("vidfunnel.encode.run_tool", side_effect=run_tool), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, calls, out.getvalue() + err.getvalue()

    def test_convert_keeps_the_finished_encode_its_sidecar_and_par2_and_stops_before_the_next_item(self):
        self.copy(SUBTITLE_SOURCE, FIRST_NAME)
        self.copy(REMUX_SOURCE, SECOND_NAME)
        plan_paths(TOOLS, [str(self.root)], make_options(), progress=quiet())
        planned = load_record(self.root / FIRST_NAME)
        self.assertEqual(planned["status"], "planned")
        sidecar_names = [Path(entry["path"]).name for entry in planned["stages"]["plan"].get("sidecars") or []]
        self.assertTrue(sidecar_names, "the subtitle fixture no longer plans a sidecar; pick another")

        code, calls, text = self.main_with_now_file_after_first_encode(["convert", "--par2", "5", str(self.root)])
        self.assertEqual(code, 1, text)
        self.assertIn("stopped (a stop file asked for it)", text)
        self.assertGreaterEqual(len(calls), 1 + len(sidecar_names), "the sidecars were not started\n" + text)
        first = load_record(self.root / FIRST_NAME)
        self.assertEqual(first["status"], "converted", text)  # saved; the stop applies again at verify's first child
        stage = first["stages"]["convert"]
        self.assertEqual(stage["outcome"], "ok", stage)
        self.assertEqual(stage["sidecars_written"], sidecar_names, stage)
        for name in sidecar_names:
            self.assertTrue((self.root / name).is_file(), name)
        output = self.root / stage["output_path"]
        self.assertTrue(output.is_file())
        self.assertEqual((stage["par2_files"] or [None])[0], output.name + ".par2", stage)
        for name in stage["par2_files"]:
            self.assertTrue((self.root / name).is_file(), name)
        self.assertEqual(first["output"]["size"], output.stat().st_size)

        second = load_record(self.root / SECOND_NAME)
        self.assertEqual(second["status"], "planned", "the item after the stop was started\n" + text)
        self.assertNotIn("convert", second.get("stages") or {})
        self.assertFalse((self.root / "second_meadow.mp4").exists())
        self.assertEqual([name for name in os.listdir(str(self.root)) if name.endswith(".part")], [])

    def test_refit_keeps_the_finished_refit_and_its_par2_and_stops_before_the_next_item(self):
        self.copy(REFIT_SOURCE, FIRST_REFIT)
        self.copy(REFIT_SOURCE, SECOND_REFIT)
        plan_paths(TOOLS, [str(self.root)], make_options(reencode_video=True), progress=quiet())
        convert_paths(TOOLS, [str(self.root)], convert_options(par2=5), quiet())
        for name in (FIRST_REFIT, SECOND_REFIT):
            record = load_record(self.root / name)
            self.assertIn("output-larger", record["flags"], "the fixture no longer re-encodes larger; pick another")
        second_before = load_record(self.root / SECOND_REFIT)
        second_output = self.root / "second_meadow.mp4"
        second_bytes = sha256_of(second_output)
        first_output = self.root / "first_harbour.mp4"
        first_before = first_output.stat().st_size

        code, calls, text = self.main_with_now_file_after_first_encode(["convert", "--refit", str(self.root)])
        self.assertEqual(code, 1, text)
        self.assertIn("stopped (a stop file asked for it)", text)
        self.assertEqual(len(calls), 1, text)
        first = load_record(self.root / FIRST_REFIT)
        refit = first["stages"]["refit"]
        self.assertEqual(refit["outcome"], "replaced", refit)
        self.assertEqual(first["status"], "converted", text)  # saved; the stop applies again at verify's first child
        self.assertEqual(first["output"]["size"], first_output.stat().st_size)
        self.assertLess(first_output.stat().st_size, first_before)
        self.assertEqual((refit.get("par2_files") or [None])[0], first_output.name + ".par2", refit)
        for name in refit["par2_files"]:
            self.assertTrue((self.root / name).is_file(), name)
        self.assertNotIn("par2-stale", first["flags"])

        second = load_record(self.root / SECOND_REFIT)
        self.assertNotIn("refit", second.get("stages") or {}, "the item after the stop was started\n" + text)
        self.assertEqual(second["status"], second_before["status"])
        self.assertEqual(sha256_of(second_output), second_bytes)
        self.assertEqual([name for name in os.listdir(str(self.root)) if name.endswith(".part")], [])

    def test_convert_with_jobs_keeps_a_finished_encode_when_another_worker_finds_the_now_file(self):
        """--jobs 2: the now-file is written as the first encode returns; the second worker finds it at its
        own encode start while the first item's PAR2 runs. That par2 is not stopped, the first item's record
        is saved 'converted' with its PAR2, and the second item is never started."""
        import threading
        import time
        import vidfunnel.ffmpeg
        real_par2 = shutil.which("par2")
        fake_bin = self.root.parent / "slow_bin"
        fake_bin.mkdir()
        wrapper = fake_bin / "par2"
        # par2 that waits a second before it works, so the stop lands while it runs.
        wrapper.write_text("#!/bin/sh\nsleep 1\nexec '{}' \"$@\"\n".format(real_par2))
        os.chmod(str(wrapper), 0o755)
        self.copy(REMUX_SOURCE, FIRST_NAME)
        self.copy(REMUX_SOURCE, SECOND_NAME)
        plan_paths(TOOLS, [str(self.root)], make_options(), progress=quiet())
        for name in (FIRST_NAME, SECOND_NAME):
            self.assertEqual(load_record(self.root / name)["status"], "planned")

        lock = threading.Lock()
        encodes = []
        first_written = threading.Event()
        problems = []

        def finishing_par2_running():
            with vidfunnel.ffmpeg._children_lock:
                return any("par2" in os.path.basename(str(process.args[0]))
                           for process in vidfunnel.ffmpeg._finishing_children)

        def run_tool(argv, *rest, **values):
            if not str(argv[-1]).endswith(".part"):
                return ffmpeg_run(argv, *rest, **values)
            with lock:
                order = len(encodes)
                encodes.append(str(argv[-1]))
            if order == 0:
                result = ffmpeg_run(argv, *rest, **values)
                self.write_now_file()
                first_written.set()
                return result
            deadline = time.monotonic() + 60
            while not (first_written.is_set() and finishing_par2_running()):
                if time.monotonic() > deadline:
                    problems.append("the first item's par2 was not seen running within 60 s")
                    break
                time.sleep(0.01)
            return ffmpeg_run(argv, *rest, **values)  # its stop file check finds 'now'

        from vidfunnel import cli
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch("vidfunnel.encode.run_tool", side_effect=run_tool), \
                unittest.mock.patch.dict(os.environ, {"PATH": "{}:{}".format(fake_bin, os.environ.get("PATH", ""))}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["convert", "--jobs", "2", "--par2", "5", str(self.root)])
        text = out.getvalue() + err.getvalue()
        self.assertEqual(problems, [], text)
        self.assertEqual(code, 1, text)
        self.assertIn("stopped (a stop file asked for it)", text)
        self.assertEqual(len(encodes), 2, text)
        records = {name: load_record(self.root / name) for name in (FIRST_NAME, SECOND_NAME)}
        finished = [name for name, record in records.items() if record["status"] != "planned"]
        self.assertEqual(len(finished), 1, text)
        first = records[finished[0]]
        self.assertEqual(first["status"], "converted", text)  # saved; the stop applied again at its verify
        stage = first["stages"]["convert"]
        self.assertEqual(stage["outcome"], "ok", stage)
        self.assertNotIn("par2-failed", first.get("flags") or [], stage)
        output = self.root / stage["output_path"]
        self.assertTrue(output.is_file())
        self.assertEqual(first["output"]["size"], output.stat().st_size)
        self.assertEqual((stage["par2_files"] or [None])[0], output.name + ".par2", stage)
        for name in stage["par2_files"]:
            self.assertTrue((self.root / name).is_file(), name)

        second_name = SECOND_NAME if finished[0] == FIRST_NAME else FIRST_NAME
        second = records[second_name]
        self.assertNotIn("convert", second.get("stages") or {}, text)
        self.assertFalse((self.root / (Path(second_name).stem + ".mp4")).exists(), text)
        self.assertEqual([name for name in os.listdir(str(self.root)) if name.endswith(".part")], [])

    def test_a_now_file_seen_before_the_main_encode_still_stops_at_that_child_start(self):
        """Written after the item's start check (check_item, as the loops call it): the encode is not started."""
        import vidfunnel.stopfile
        self.copy(SUBTITLE_SOURCE, FIRST_NAME)
        plan_paths(TOOLS, [str(self.root)], make_options(), progress=quiet())
        record = load_record(self.root / FIRST_NAME)
        self.assertIsNone(vidfunnel.stopfile.check_item(str(self.root)))
        self.write_now_file()
        with unittest.mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                convert_one(TOOLS, self.root / FIRST_NAME, record, convert_options(), progress=quiet())
        self.assertEqual(load_record(self.root / FIRST_NAME)["status"], "planned")
        self.assertFalse((self.root / "first_harbour.mp4").exists())
        self.assertEqual([name for name in os.listdir(str(self.root)) if name.endswith((".part", ".srt"))], [])

    def test_a_now_file_seen_before_the_refit_encode_still_stops_at_that_child_start(self):
        import vidfunnel.stopfile
        self.copy(REFIT_SOURCE, FIRST_REFIT)
        plan_paths(TOOLS, [str(self.root)], make_options(reencode_video=True), progress=quiet())
        convert_paths(TOOLS, [str(self.root)], convert_options(), quiet())
        output = self.root / "first_harbour.mp4"
        before = sha256_of(output)
        key = self.root / FIRST_REFIT
        self.assertIsNone(vidfunnel.stopfile.check_item(str(self.root)))
        self.write_now_file()
        with unittest.mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                refit_one(TOOLS, key, load_record(key), convert_options(refit=True), quiet())
        self.assertNotIn("refit", load_record(key).get("stages") or {})
        self.assertEqual(sha256_of(output), before)
        self.assertEqual([name for name in os.listdir(str(self.root)) if name.endswith(".part")], [])


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
