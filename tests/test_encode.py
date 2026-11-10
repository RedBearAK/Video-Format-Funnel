"""Tests for vidfunnel.encode: temp names, skip rules, and convert runs on planned fixture copies.

The pure tests need no ffmpeg. The integration tests copy fixtures from
tests/fixtures into a temporary folder, plan them with vidfunnel.plan, and
convert the copies (never the fixture folder itself); they skip with a
message when ffmpeg or the fixtures are missing. Whether a converted record
ends at 'converted' or 'verified' depends on whether vidfunnel.verify is
present, so both are accepted.
"""

VERSION = "20261110"

import argparse
import contextlib
import errno
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
import vidfunnel.interrupt
import vidfunnel.record as record_module
import vidfunnel.turns
from vidfunnel import encode
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
        return {"status": status, "source": {"name": "movie.avi"},
                "output": {"name": "movie.mp4", "fingerprint": fingerprint}}

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

    def test_an_output_time_that_differs_alone_is_still_already_converted(self):
        os.utime(str(self.output), ns=(PLANNED_MTIME_NS, PLANNED_MTIME_NS))
        fingerprint = cheap_fingerprint(self.output)
        for view, seen_ns in OTHER_HOST_VIEWS:
            with self.subTest(view=view):
                os.utime(str(self.output), ns=(seen_ns, seen_ns))
                self.assertEqual(skip_reason(self.record("verified", fingerprint), self.output), "already converted")

    def test_output_content_that_differs_is_refused_as_changed_since(self):
        # A changed output is not 'already converted'; convert refuses the record (it is not 'planned')
        # and asks for plan --replan, as before the time rule.
        self.output.write_bytes(LARGE_CONTENT)
        os.utime(str(self.output), ns=(PLANNED_MTIME_NS, PLANNED_MTIME_NS))
        fingerprint = cheap_fingerprint(self.output)
        expected = ("status is 'converted' but the output 'movie.mp4' is missing or changed since; to convert "
                    "again, plan it again with: vidfunnel plan --replan '{}'".format(self.output.parent / "movie.avi"))
        for change, edit, _words in CONTENT_CHANGES:
            with self.subTest(change=change):
                edit(self.output)
                os.utime(str(self.output), ns=(PLANNED_MTIME_NS, PLANNED_MTIME_NS))
                record = self.record("converted", fingerprint)
                self.assertIsNone(skip_reason(record, self.output))
                self.assertEqual(encode._refusal(record, self.output.parent, self.output), expected)
                self.output.write_bytes(LARGE_CONTENT)


# The time a file shows on the planning host, and what other hosts' mounts report for it.
PLANNED_MTIME_NS = 1700000001987654321
OTHER_HOST_VIEWS = (
    ("hundred-nanosecond steps", 1700000001987654300),
    ("whole seconds", 1700000001000000000),
    ("two-second steps", 1700000000000000000),
    ("days later", PLANNED_MTIME_NS + 3 * 86400 * 1000 * 1000 * 1000),
)
LARGE_CONTENT = bytes(range(256)) * (3 * 1024 * 1024 // 256)   # 3 MiB, so the first and last MiB differ
SECOND_NS = 1000 * 1000 * 1000
STEPS = (("hundred_nanoseconds", 100), ("microsecond", 1000), ("millisecond", 1000 * 1000),
         ("ten_milliseconds", 10 * 1000 * 1000), ("second", SECOND_NS), ("two_seconds", 2 * SECOND_NS))


def truncated(value, step):
    return value - value % step


# (word, recorded time, seen time) of a source: one is the other truncated to a coarser step, in both directions.
COARSER_VIEWS = ([("seen_" + word, PLANNED_MTIME_NS, truncated(PLANNED_MTIME_NS, step)) for word, step in STEPS]
                 + [("recorded_" + word, truncated(PLANNED_MTIME_NS, step), PLANNED_MTIME_NS) for word, step in STEPS])
# (word, recorded time, seen time): differences that no truncation explains.
OTHER_TIMES = (
    ("second_later", PLANNED_MTIME_NS, PLANNED_MTIME_NS + SECOND_NS),
    ("second_and_half_later", PLANNED_MTIME_NS, PLANNED_MTIME_NS + 3 * SECOND_NS // 2),
    ("days_later", PLANNED_MTIME_NS, PLANNED_MTIME_NS + 3 * 86400 * SECOND_NS),
)


def changed_middle(path):
    """Flip one byte in the middle of a file of LARGE_CONTENT: size and first and last MiB stay the same."""
    content = bytearray(path.read_bytes())
    content[len(content) // 2] ^= 0xFF
    path.write_bytes(bytes(content))


def _flip_byte_at(offset):
    def flip(path):
        content = bytearray(path.read_bytes())
        content[offset] ^= 0xFF
        path.write_bytes(bytes(content))
    return flip


def _append_bytes(path):
    with open(str(path), "ab") as handle:
        handle.write(b"appended")


# Each entry: (what differs, the edit, the words convert's reason names it with).
CONTENT_CHANGES = (
    ("size", _append_bytes, "size {} bytes recorded, {} now".format(len(LARGE_CONTENT), len(LARGE_CONTENT) + 8)),
    ("first MiB", _flip_byte_at(0), "(first MiB differs)"),
    ("last MiB", _flip_byte_at(-1), "(last MiB differs)"),
)


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
    ("file", ["stages", "probe", "video", "fps"], 1.7e308),
    ("file", ["stages", "probe", "video", "fps"], 1000000.5),
    ("file", ["stages", "probe", "video", "avg_fps"], 1000001.0),
    ("file", ["stages", "probe", "video", "avg_fps"], 1.7e308),
    ("file", ["stages", "probe", "video", "fps"], 10 ** 400),
    ("file", ["stages", "probe", "video", "avg_fps"], 10 ** 400),
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
    ("file", CADENCE + ["output_fps"], "1000001/1"),
    ("file", CADENCE + ["output_fps"], "9999999999/1"),
    ("file", CADENCE + ["fps_multiplier"], "2.0"),
    ("file", CADENCE + ["fps_multiplier"], 3.0),
    ("file", CADENCE + ["fps_multiplier"], True),
    ("file", CADENCE + ["fps_multiplier"], 10 ** 400),
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
    ("file", FIRST_SYNC + ["offset"], 10 ** 400),
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
        del record["output"]["sidecars"]
        save_record(key, record)
        self.assertIsNone(record_value_problem(record))
        self.assertFalse((folder / "h264_mkv_srt_subtitles.srt").exists())
        self.assertEqual(render_record(self.tools, folder, record)["sidecars"], [])
        record["stages"]["plan"]["sidecars_skipped"] = []
        record["output"]["sidecars"] = ["h264_mkv_srt_subtitles.srt"]
        (folder / "h264_mkv_srt_subtitles.srt").write_text("a file that appeared after plan\n")
        rendered = render_record(self.tools, folder, record)["sidecars"]
        self.assertEqual([entry["path"] for entry in rendered], [str(folder / "h264_mkv_srt_subtitles.srt")])

    def test_a_record_listing_fewer_sidecars_than_the_analysis_gives_renders_only_those(self):
        # A record planned before 2026-11-07: an .srt only for styled streams, so none for this subrip stream.
        folder = self.folder()
        _key, record = captured_record(folder, "h264_mkv_srt_subtitles.mkv", "remux")
        self.assertEqual(record["output"]["sidecars"], ["h264_mkv_srt_subtitles.srt"])
        record["output"]["sidecars"] = []
        self.assertIsNone(record_value_problem(record))
        rendered = render_record(self.tools, folder, record)
        self.assertEqual(rendered["sidecars"], [])
        self.assertIn("mov_text", rendered["argv"])

    def test_an_older_subtitle_record_flagged_larger_is_selected_and_rendered_by_refit(self):
        # Refit writes no sidecars, so no sidecar list blocks it: the older record's empty one, or a stranger name.
        for listed in ([], ["h264_mkv_srt_subtitles.2.srt"]):
            with self.subTest(sidecars=listed):
                folder = self.folder()
                key, record = captured_record(folder, "h264_mkv_srt_subtitles.mkv", "reencode", stand_in=self.stored)
                record["output"]["sidecars"] = listed
                set_status(record, "converted")
                record["flags"] = ["output-larger"]
                source_size = (folder / "h264_mkv_srt_subtitles.mkv").stat().st_size
                (folder / "h264_mkv_srt_subtitles.mp4").write_bytes(b"larger converted output " * 400)
                record["stages"]["convert"] = {"outcome": "ok", "source_size": source_size, "output_size": None,
                                               "argv": [str(self.stand_in), "-c:v", "libx264", "-crf", "18", "-f",
                                                        "mp4", str(folder / "h264_mkv_srt_subtitles.mp4")]}
                save_record(key, record)
                record_bytes = record_path(key).read_bytes()
                _summary, text = self.convert(folder, refit=True, dry_run=True)
                self.assertIn("would refit 'h264_mkv_srt_subtitles.mkv': CRF 18 -> ", text)
                self.assertNotIn("would skip", text)
                self.assertEqual(record_path(key).read_bytes(), record_bytes)
                self.assertEqual([call for call in self.calls_made() if call != "version"], [], text)

    def test_a_record_listing_a_sidecar_the_analysis_does_not_give_is_skipped(self):
        folder, key = self.saved("h264_mkv_srt_subtitles.mkv", "remux",
                                 change=ShapeSetter(["output", "sidecars"],
                                                    ["h264_mkv_srt_subtitles.srt", "h264_mkv_srt_subtitles.sup"]))
        self.convert_untouched(folder, key, None,
                               "output.sidecars lists 'h264_mkv_srt_subtitles.srt', 'h264_mkv_srt_subtitles.sup' "
                               "but the record's analysis gives 'h264_mkv_srt_subtitles.srt'")

    def test_a_marked_record_listing_an_interim_sidecar_the_rendering_does_not_give_is_skipped(self):
        folder = self.folder()
        key, record = captured_record(folder, "h264_mkv_srt_subtitles.mkv", "remux", stand_in=self.stored)
        record["stages"]["plan"]["mark_outputs"] = True
        record["output"]["interim_name"] = "h264_mkv_srt_subtitles.vidfunnel.mp4"
        record["output"]["interim_sidecars"] = ["h264_mkv_srt_subtitles.vidfunnel.2.srt"]
        save_record(key, record)
        self.convert_untouched(folder, key, None,
                               "output.interim_sidecars lists 'h264_mkv_srt_subtitles.vidfunnel.2.srt' but the "
                               "record's analysis gives 'h264_mkv_srt_subtitles.vidfunnel.srt'")

    def test_a_marked_record_listing_its_interim_sidecar_reaches_ffmpeg(self):
        folder = self.folder()
        key, record = captured_record(folder, "h264_mkv_srt_subtitles.mkv", "remux", stand_in=self.stored)
        record["stages"]["plan"]["mark_outputs"] = True
        record["output"]["interim_name"] = "h264_mkv_srt_subtitles.vidfunnel.mp4"
        record["output"]["interim_sidecars"] = ["h264_mkv_srt_subtitles.vidfunnel.srt"]
        rendered = render_record(self.tools, folder, record)
        self.assertEqual([entry["path"] for entry in rendered["sidecars"]],
                         [str(folder / "h264_mkv_srt_subtitles.vidfunnel.srt")])

    def test_a_source_rate_times_the_multiplier_above_the_ceiling_is_skipped(self):
        class BothChanges(object):
            def apply(self, record):
                record["stages"]["analyze"]["cadence"]["result"]["output_fps"] = None
                record["stages"]["probe"]["video"]["fps"] = 600000.0
        folder, key = self.saved("h264_1080i_tff_ts_ac3.ts", "reencode", change=BothChanges())
        self.convert_untouched(folder, key, None, "stages.analyze.cadence.result.fps_multiplier is 2.0 (type float), "
                                                  "not a multiplier that keeps the source rate times it at most "
                                                  "1000000 ")

    def test_a_timebase_like_frame_rate_from_a_real_stream_is_converted(self):
        # ffprobe's r_frame_rate of MPEG-TS, ASF and variable-rate MKV streams can read 90000/1.
        def from_fps(record):
            record["stages"]["probe"]["video"]["fps"] = 90000.0
            record["stages"]["analyze"]["cadence"]["result"]["output_fps"] = None

        def from_output_fps(record):
            record["stages"]["analyze"]["cadence"]["result"]["output_fps"] = "90000/1"

        for change in (from_fps, from_output_fps):
            with self.subTest(change=change.__name__):
                folder = self.folder()
                key, record = captured_record(folder, "xvid_avi_mp3.avi", "reencode", stand_in=self.stored)
                change(record)
                self.assertIsNone(record_value_problem(record))
                argv = render_record(self.tools, folder, record)["argv"]
                self.assertEqual(argv[argv.index("-g") + 1], "180000")
                save_record(key, record)
                summary, text = self.convert(folder)
                self.assertEqual(summary.counts["skipped"], 0, text)
                self.assertEqual(summary.counts["failed"], 1, text)
                self.assertIn("run {}".format(part_path(folder / "xvid_avi_mp3.mp4", FIRST_RECORD_ID)),
                              self.calls_made())

    def test_an_arithmetic_error_while_rendering_skips_the_record_and_the_run_goes_on(self):
        folder = self.folder()
        for fixture, record_id in (("xvid_avi_mp3.avi", FIRST_RECORD_ID), ("h264_avi_mp3.avi", SECOND_RECORD_ID)):
            key, record = captured_record(folder, fixture, "reencode", stand_in=self.stored)
            record["id"] = record_id
            save_record(key, record)
        first_bytes = record_path(folder / "xvid_avi_mp3.avi").read_bytes()

        def overflowing(tools, where, record, *rest, **values):
            if record["id"] == FIRST_RECORD_ID:
                raise OverflowError("cannot convert float infinity to integer")
            return render_record(tools, where, record, *rest, **values)

        with unittest.mock.patch("vidfunnel.encode.render_record", side_effect=overflowing):
            summary, text = self.convert(folder)
        self.assertEqual(summary.counts["skipped"], 1, text)
        self.assertEqual(summary.counts["failed"], 1, text)
        self.assertIn("skipping 'xvid_avi_mp3.avi': the command cannot be rendered from the record: cannot convert "
                      "float infinity to integer", text)
        self.assertIn("made-up stand-in failure", text)
        self.assertEqual(record_path(folder / "xvid_avi_mp3.avi").read_bytes(), first_bytes)
        self.assertEqual(load_record(folder / "h264_avi_mp3.avi")["status"], "failed")

    def test_record_shapes_that_crashed_before_the_checks_are_skipped_in_both_modes(self):
        for kind, path, value in (("file", ["stages"], 7), ("file", ["output"], "xvid_avi_mp3.mp4"),
                                  ("file", ["output"], ["xvid_avi_mp3.mp4"]), ("vob-group", ["source", "members"], 5)):
            for dry_run in (True, False):
                change = ShapeSetter(path, value)
                with self.subTest(field=change.field(), value=value, dry_run=dry_run):
                    folder = self.folder()
                    key, record = captured_record(folder, "xvid_avi_mp3.avi", "reencode", kind, self.stored)
                    change.apply(record)
                    save_record(key, record)
                    good_key, good = captured_record(folder, "h264_avi_mp3.avi", "reencode", stand_in=self.stored)
                    good["id"] = SECOND_RECORD_ID
                    save_record(good_key, good)
                    record_bytes = record_path(key).read_bytes()
                    summary, text = self.convert(folder, dry_run=dry_run)
                    self.assertRegex(text, r"skipping a record in '[^']+': {} is ".format(re.escape(change.field())))
                    self.assertEqual(summary.counts["skipped"], 1, text)
                    self.assertEqual(record_path(key).read_bytes(), record_bytes)
                    if dry_run:
                        self.assertIn("would re-encode 'h264_avi_mp3.avi'", text)
                    else:
                        self.assertEqual(summary.counts["failed"], 1, text)
                        self.assertIn("made-up stand-in failure", text)
                        self.assertEqual(load_record(good_key)["status"], "failed")

    def test_an_integer_too_large_for_a_float_skips_the_record_and_the_run_goes_on(self):
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                folder = self.folder()
                key, record = captured_record(folder, "xvid_avi_mp3.avi", "reencode", stand_in=self.stored)
                record["stages"]["probe"]["video"]["fps"] = 10 ** 400
                save_record(key, record)
                good_key, good = captured_record(folder, "h264_avi_mp3.avi", "reencode", stand_in=self.stored)
                good["id"] = SECOND_RECORD_ID
                save_record(good_key, good)
                record_bytes = record_path(key).read_bytes()
                summary, text = self.convert(folder, dry_run=dry_run)
                self.assertIn("{} 'xvid_avi_mp3.avi': stages.probe.video.fps is 1000".format(
                    "would skip" if dry_run else "skipping"), text)
                self.assertIn("... (type int), not a finite positive number", text)
                self.assertNotIn("0" * 100, text)
                self.assertEqual(record_path(key).read_bytes(), record_bytes)
                if dry_run:
                    self.assertIn("would re-encode 'h264_avi_mp3.avi'", text)
                else:
                    self.assertEqual(summary.counts["skipped"], 1, text)
                    self.assertEqual(summary.counts["failed"], 1, text)
                    self.assertIn("made-up stand-in failure", text)
                    self.assertEqual(load_record(good_key)["status"], "failed")

    def test_an_arithmetic_error_while_checking_values_skips_the_record(self):
        folder, key = self.saved("xvid_avi_mp3.avi", "reencode")
        record_bytes = record_path(key).read_bytes()
        with unittest.mock.patch("vidfunnel.encode.record_value_problem",
                                 side_effect=OverflowError("int too large to convert to float")):
            summary, text = self.convert(folder)
        self.assertEqual(summary.counts["skipped"], 1, text)
        self.assertIn("skipping 'xvid_avi_mp3.avi': the record's values cannot be checked: int too large to "
                      "convert to float", text)
        self.assertEqual(record_path(key).read_bytes(), record_bytes)
        self.assertEqual(self.calls_made(), [], text)

    def test_missing_mov_text_encoder_skips_the_record(self):
        folder, key = self.saved("h264_mkv_srt_subtitles.mkv", "remux")
        self.convert_untouched(folder, key, listing_without("encoders", "mov_text"),
                               "ffmpeg '{}' has no encoder 'mov_text'".format(self.stand_in))

    def test_missing_sidecar_encoder_skips_the_record(self):
        folder, key = self.saved("h264_mkv_srt_subtitles.mkv", "remux")
        self.convert_untouched(folder, key, listing_without("encoders", "srt"),
                               "ffmpeg '{}' has no encoder 'srt'".format(self.stand_in))

    def test_missing_sidecar_muxer_skips_the_record(self):
        folder, key = self.saved("h264_mkv_srt_subtitles.mkv", "remux")
        self.convert_untouched(folder, key, listing_without("muxers", "srt"),
                               "ffmpeg '{}' has no muxer 'srt'".format(self.stand_in))

    def test_argv_needs(self):
        argv = ["ffmpeg", "-f", "dvdvideo", "-title", "1", "-i", "/disc", "-map", "0:v:0",
                "-vf", "bwdif=mode=1:parity=0,setparams=range=tv", "-c:v", "libx264", "-c:a:0", "copy",
                "-c:a:1", "aac", "-filter:a:1", "aresample=async=1,atempo=1.01", "-bsf:v", "h264_metadata=x=1",
                "-c:s", "mov_text", "-f", "mp4", "/out.mp4"]
        self.assertEqual(argv_needs(argv), [("demuxers", "dvdvideo"), ("filters", "bwdif"), ("filters", "setparams"),
                                            ("encoders", "libx264"), ("encoders", "aac"), ("filters", "aresample"),
                                            ("filters", "atempo"), ("bsfs", "h264_metadata"),
                                            ("encoders", "mov_text"), ("muxers", "mp4")])
        sidecar = ["ffmpeg", "-i", "/in.mkv", "-map", "0:s:0", "-c:s", "srt", "-f", "srt", "/out.srt"]
        self.assertEqual(argv_needs(sidecar), [("encoders", "srt"), ("muxers", "srt")])
        copied = ["ffmpeg", "-i", "/in.mkv", "-map", "0:s:0", "-c:s", "copy", "-f", "sup", "/out.sup"]
        self.assertEqual(argv_needs(copied), [("muxers", "sup")])

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


def succeeding_ffmpeg(path):
    """An executable at `path` printing an ffmpeg -version banner, else writing a few bytes to its last argument."""
    path.write_text("#!/bin/sh\n"
                    "case \"$*\" in *-version*)\n"
                    "  echo 'ffmpeg version {version} Copyright (c) 2000-2026 the FFmpeg developers'\n"
                    "  echo 'configuration: --enable-gpl'; exit 0;; esac\n"
                    "for last in \"$@\"; do :; done\n"
                    "printf 'stand-in output' > \"$last\"\n"
                    "exit 0\n".format(version=STAND_IN_FFMPEG_VERSION))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class ConvertSourceTimeTest(unittest.TestCase):
    """The source is checked before anything is written: a coarser view of its time reads nothing, another time
    reads it once; a source that changed is skipped with its record left as it is."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        stand_in = succeeding_ffmpeg(self.base / "ffmpeg")
        self.tools = types.SimpleNamespace(ffmpeg=str(stand_in), ffprobe=str(stand_in))
        self.folders = 0

    def folder(self):
        self.folders += 1
        folder = self.base / "library{}".format(self.folders)
        folder.mkdir()
        return folder

    def planned(self, folder, fixture, record_id=FIRST_RECORD_ID, recorded_ns=PLANNED_MTIME_NS):
        """A planned record whose 3 MiB source carried recorded_ns when it was fingerprinted."""
        key, record = captured_record(folder, fixture, "reencode")
        source = folder / fixture
        source.write_bytes(LARGE_CONTENT)
        os.utime(str(source), ns=(recorded_ns, recorded_ns))
        record["id"] = record_id
        record["source"].update(size=len(LARGE_CONTENT), fingerprint=cheap_fingerprint(source),
                                sha256=sha256_of(source))
        save_record(key, record)
        return key, source

    def convert(self, folder, quiet_run=True):
        progress = quiet() if quiet_run else types.SimpleNamespace(stream=io.StringIO(), quiet=False, verbose=False)
        with unittest.mock.patch("vidfunnel.encode.ffmpeg_capabilities", return_value=FULL_LISTING), \
                unittest.mock.patch("vidfunnel.encode._verify", side_effect=lambda tools, key, record, *rest: record), \
                unittest.mock.patch("vidfunnel.record.full_sha256", wraps=record_module.full_sha256) as hasher:
            summary = convert_paths(self.tools, [str(folder)], convert_options(), progress)
        self.hasher = hasher
        return summary, progress.stream.getvalue()

    def assert_converted_with_note(self, folder, key, source, recorded_ns, seen_ns, read):
        summary, text = self.convert(folder)
        self.assertEqual(summary.counts["converted"], 1, text)
        self.assertEqual(summary.counts["skipped"] + summary.counts["failed"], 0, text)
        self.assertEqual(self.hasher.call_count, 1 if read else 0)
        saved = load_record(key)
        self.assertEqual(saved["status"], "converted")
        self.assertNotIn("source-changed", saved["flags"])
        stage = saved["stages"]["convert"]
        self.assertEqual(stage["outcome"], "ok")
        self.assertEqual(stage["source_mtime_differs"], [{"name": source.name, "recorded": recorded_ns,
                                                          "seen": seen_ns}])
        self.assertIs(stage["source_sha256_checked"], read)
        self.assertEqual(saved["source"]["fingerprint"]["mtime_ns"], recorded_ns)
        # The output gets the source's time as this host sees it, and the record holds what was stored.
        output = folder / (Path(source.name).stem + ".mp4")
        self.assertEqual(output.stat().st_mtime_ns, seen_ns)
        self.assertEqual(saved["output"]["fingerprint"], cheap_fingerprint(output))

    def test_a_coarser_view_of_the_source_time_converts_without_a_read(self):
        for word, recorded_ns, seen_ns in COARSER_VIEWS:
            with self.subTest(case=word):
                folder = self.folder()
                key, source = self.planned(folder, REENCODE_SOURCE, recorded_ns=recorded_ns)
                os.utime(str(source), ns=(seen_ns, seen_ns))
                self.assert_converted_with_note(folder, key, source, recorded_ns, seen_ns, read=False)

    def test_another_source_time_with_the_same_content_is_read_once_and_converted(self):
        for word, recorded_ns, seen_ns in OTHER_TIMES:
            with self.subTest(case=word):
                folder = self.folder()
                key, source = self.planned(folder, REENCODE_SOURCE, recorded_ns=recorded_ns)
                os.utime(str(source), ns=(seen_ns, seen_ns))
                self.assert_converted_with_note(folder, key, source, recorded_ns, seen_ns, read=True)

    def test_another_source_time_with_a_changed_middle_is_skipped_and_nothing_written(self):
        for word, recorded_ns, seen_ns in OTHER_TIMES:
            with self.subTest(case=word):
                folder = self.folder()
                key, source = self.planned(folder, REENCODE_SOURCE, recorded_ns=recorded_ns)
                changed_middle(source)
                os.utime(str(source), ns=(seen_ns, seen_ns))
                record_bytes = record_path(key).read_bytes()
                before = listing(folder)
                summary, text = self.convert(folder)
                self.assertEqual(summary.counts["skipped"], 1, text)
                self.assertEqual(summary.counts["converted"] + summary.counts["failed"], 0, text)
                self.assertEqual(self.hasher.call_count, 1)
                self.assertEqual(record_path(key).read_bytes(), record_bytes)
                self.assertEqual(listing(folder), before)
                self.assertIn("skipping '{}': source changed since plan: SHA-256 of '{}' is {}, the record says {}; "
                              "plan it again with: vidfunnel plan '{}'".format(
                                  REENCODE_SOURCE, REENCODE_SOURCE, sha256_of(source),
                                  load_record(key)["source"]["sha256"], source), text)

    def test_a_record_without_a_source_hash_is_skipped_before_any_read(self):
        # Records always carry source.sha256; one without it fails the value check before the source is read.
        folder = self.folder()
        key, source = self.planned(folder, REENCODE_SOURCE)
        record = load_record(key)
        record["source"]["sha256"] = None
        save_record(key, record)
        os.utime(str(source), ns=(PLANNED_MTIME_NS + SECOND_NS, PLANNED_MTIME_NS + SECOND_NS))
        record_bytes = record_path(key).read_bytes()
        summary, text = self.convert(folder)
        self.assertEqual(summary.counts["skipped"], 1, text)
        self.hasher.assert_not_called()
        self.assertIn("source.sha256", text)
        self.assertEqual(record_path(key).read_bytes(), record_bytes)

    def test_the_same_time_writes_no_note(self):
        folder = self.folder()
        key, _source = self.planned(folder, REENCODE_SOURCE)
        summary, text = self.convert(folder)
        self.assertEqual(summary.counts["converted"], 1, text)
        self.hasher.assert_not_called()
        self.assertNotIn("source_mtime_differs", load_record(key)["stages"]["convert"])
        self.assertNotIn("source_sha256_checked", load_record(key)["stages"]["convert"])

    def test_a_full_read_is_announced(self):
        folder = self.folder()
        _key, source = self.planned(folder, REENCODE_SOURCE)
        os.utime(str(source), ns=(PLANNED_MTIME_NS + SECOND_NS, PLANNED_MTIME_NS + SECOND_NS))
        _summary, text = self.convert(folder, quiet_run=False)
        self.assertIn("[convert 1/1]   reading 'xvid_avi_mp3.avi' in full (3.0 MiB): its modification time is "
                      "neither the recorded one nor a coarser view of it", text)

    def test_a_vob_member_whose_time_differs_is_read_with_the_group_and_named_in_the_note(self):
        folder = self.folder()
        key, record = captured_record(folder, REENCODE_SOURCE, "reencode", kind="vob-group")
        save_record(key, record)
        second = folder / "VTS_01_2.VOB"
        recorded_ns = record["source"]["members"][1]["fingerprint"]["mtime_ns"]
        os.utime(str(second), ns=(PLANNED_MTIME_NS, PLANNED_MTIME_NS))
        summary, text = self.convert(folder)
        self.assertEqual(summary.counts["converted"], 1, text)
        self.assertEqual(self.hasher.call_args[0][0], (str(folder / "VTS_01_1.VOB"), str(second)))
        stage = load_record(key)["stages"]["convert"]
        self.assertEqual(stage["source_mtime_differs"],
                         [{"name": "VTS_01_2.VOB", "recorded": recorded_ns, "seen": PLANNED_MTIME_NS}])
        self.assertIs(stage["source_sha256_checked"], True)

    def test_the_output_collision_stage_carries_the_note(self):
        folder = self.folder()
        key, source = self.planned(folder, REENCODE_SOURCE)
        whole_seconds = truncated(PLANNED_MTIME_NS, SECOND_NS)
        os.utime(str(source), ns=(whole_seconds, whole_seconds))
        write_bytes(folder / "xvid_avi_mp3.mp4", b"a file that is not ours")
        with unittest.mock.patch("vidfunnel.encode._ours", return_value=False):
            summary, text = self.convert(folder)
        self.assertEqual(summary.counts["failed"], 1, text)
        stage = load_record(key)["stages"]["convert"]
        self.assertEqual(stage["outcome"], "output-collision")
        self.assertEqual(stage["source_mtime_differs"],
                         [{"name": REENCODE_SOURCE, "recorded": PLANNED_MTIME_NS, "seen": whole_seconds}])
        self.assertIs(stage["source_sha256_checked"], False)

    def test_source_content_that_differs_is_skipped_and_the_record_left_as_it_is(self):
        for change, edit, words in CONTENT_CHANGES:
            with self.subTest(change=change):
                folder = self.folder()
                key, source = self.planned(folder, REENCODE_SOURCE)
                good_key, _good_source = self.planned(folder, REFIT_SOURCE, SECOND_RECORD_ID)
                edit(source)
                os.utime(str(source), ns=(PLANNED_MTIME_NS, PLANNED_MTIME_NS))
                record_bytes = record_path(key).read_bytes()
                summary, text = self.convert(folder)
                self.assertEqual(summary.counts["skipped"], 1, text)
                self.assertEqual(summary.counts["converted"], 1, text)
                self.assertEqual(summary.counts["failed"], 0, text)
                self.assertEqual(record_path(key).read_bytes(), record_bytes)
                self.assertFalse((folder / "xvid_avi_mp3.mp4").exists())
                self.assertIn("skipping '{}': source changed since plan: '{}' differs from the fingerprint taken "
                              "at plan (".format(REENCODE_SOURCE, REENCODE_SOURCE), text)
                self.assertIn(words, text)
                self.assertIn("; plan it again with: vidfunnel plan '{}'".format(source), text)
                self.assertEqual(load_record(good_key)["status"], "converted")
                self.assertTrue((folder / "h264_avi_mp3.mp4").is_file())


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

    def test_a_leftover_part_file_still_written_is_unlinked_and_never_mixed_in(self):
        """An encoder orphaned by a killed run still holds the part file; the new encode writes a new inode."""
        record = self.plan(REMUX_SOURCE)[REMUX_SOURCE]
        leftover = part_path(self.root / "h264_high_mkv_aac.mp4", record["id"])
        leftover.write_bytes(b"the orphan's first bytes")
        leftover_inode = leftover.stat().st_ino
        marker = b"WRITTEN BY THE ORPHANED ENCODER AFTER THE NEW ENCODE"
        writer = subprocess.Popen(
            [sys.executable, "-c", "import os, sys\nstream = open(sys.argv[1], 'ab')\nprint('ready', flush=True)\n"
             "sys.stdin.readline()\nstream.write(sys.argv[2].encode())\nstream.flush()\nos.fsync(stream.fileno())\n",
             str(leftover), marker.decode()],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, universal_newlines=True)
        self.addCleanup(writer.stdout.close)
        self.addCleanup(writer.stdin.close)
        self.addCleanup(writer.wait)
        self.addCleanup(writer.kill)
        self.assertEqual(writer.stdout.readline(), "ready\n")
        seen = {}

        def encode_beside_the_orphan(argv, *rest, **values):
            seen["existed"] = os.path.exists(argv[-1])
            completed = ffmpeg_run(argv, *rest, **values)
            seen["inode"] = os.stat(argv[-1]).st_ino
            writer.stdin.write("go\n")
            writer.stdin.flush()
            writer.wait(20)
            return completed

        with unittest.mock.patch("vidfunnel.encode.run_tool", side_effect=encode_beside_the_orphan):
            stage = self.convert(REMUX_SOURCE, record)
        self.assertEqual(stage["outcome"], "ok", stage)
        self.assertEqual(writer.returncode, 0)
        self.assertFalse(seen["existed"], "the leftover part file was still there when ffmpeg started")
        self.assertNotEqual(seen["inode"], leftover_inode)
        output = self.root / "h264_high_mkv_aac.mp4"
        self.assertEqual(output.stat().st_ino, seen["inode"])
        content = output.read_bytes()
        self.assertNotIn(marker, content)
        self.assertNotIn(b"the orphan's first bytes", content)

    def test_a_leftover_part_file_that_cannot_be_removed_skips_the_item(self):
        record = self.plan(REMUX_SOURCE)[REMUX_SOURCE]
        leftover = part_path(self.root / "h264_high_mkv_aac.mp4", record["id"])
        leftover.write_bytes(b"left by an earlier run")
        record_bytes = record_path(self.root / REMUX_SOURCE).read_bytes()
        real_unlink = os.unlink

        def refuse(path, *rest, **values):
            if os.fspath(path) == str(leftover):
                raise PermissionError(13, "Permission denied", str(path))
            return real_unlink(path, *rest, **values)

        progress = quiet()
        with unittest.mock.patch("vidfunnel.encode.os.unlink", side_effect=refuse), \
                unittest.mock.patch("vidfunnel.encode.run_tool", side_effect=AssertionError("ffmpeg started")):
            stage = convert_one(TOOLS, self.root / REMUX_SOURCE, record, convert_options(), progress=progress)
        self.assertEqual(stage["outcome"], "skipped", stage)
        self.assertIn("cannot remove the part file '{}' left by an earlier run (Permission denied)".format(
            leftover.name), stage["skipped"])
        self.assertEqual(record_path(self.root / REMUX_SOURCE).read_bytes(), record_bytes)
        self.assertEqual(leftover.read_bytes(), b"left by an earlier run")
        self.assertFalse((self.root / "h264_high_mkv_aac.mp4").exists())
        self.assertIn("skipping 'h264_high_mkv_aac.mkv': cannot remove the part file", progress.stream.getvalue())

    def taken_over_after_the_encode(self, fail=False, silent_stat=False):
        """run_tool for convert_paths: A's ffmpeg runs, then host B takes the folder and starts the same record.

        B replaces the lock file, clears A's part file as a leftover and writes its own partial output there.
        Returns (patch, seen); seen gets B's part path, its bytes and the record's bytes as B left them.
        """
        seen = {}
        lock_file = self.root / ".vidfunnel" / "lock"

        def encode_then_host_b(argv, *rest, **values):
            completed = ffmpeg_run(argv, *rest, **values)
            part = Path(argv[-1])
            if silent_stat:
                seen["unreachable"] = str(lock_file)
            else:
                os.unlink(str(lock_file))
                lock_file.write_text(json.dumps({"host": "hostb", "pid": 4242, "started": "2026-11-09T12:00:00Z",
                                                 "heartbeat": {"interval": 300}}), encoding="utf-8")
                self.assertEqual(encode._clear_leftover(part), (True, None))
                part.write_bytes(b"PARTIAL OUTPUT OF HOST B, STILL BEING WRITTEN")
            seen.update(part=part, part_bytes=part.read_bytes(), record_bytes=record_path(self.root / REMUX_SOURCE)
                        .read_bytes())
            if fail:
                raise FfmpegError("ffmpeg failed after the takeover", argv=argv, returncode=1, stderr="simulated")
            return completed

        return unittest.mock.patch("vidfunnel.encode.run_tool", side_effect=encode_then_host_b), seen

    def quiet_lost_lock(self):
        self.addCleanup(vidfunnel.interrupt.draining.clear)
        self.addCleanup(vidfunnel.interrupt.drain_tally.reset)
        lines = []
        patch = unittest.mock.patch("vidfunnel.record._warn", side_effect=lines.append)
        patch.start()
        self.addCleanup(patch.stop)
        return lines

    def assert_nothing_written_after_the_takeover(self, seen, summary, text, reason):
        output = self.root / "h264_high_mkv_aac.mp4"
        self.assertFalse(output.exists(), "the old holder put a file in place as the output")
        self.assertEqual(seen["part"].read_bytes(), seen["part_bytes"], "the part file at the path was touched")
        self.assertEqual(record_path(self.root / REMUX_SOURCE).read_bytes(), seen["record_bytes"])
        self.assertEqual(summary.counts.get("skipped"), 1, text)
        self.assertIn("skipping 'h264_high_mkv_aac.mkv': the lock of folder '{}' {}".format(self.root, reason), text)

    def test_a_holder_whose_lock_was_taken_during_the_encode_writes_nothing(self):
        self.plan(REMUX_SOURCE)
        lines = self.quiet_lost_lock()
        patch, seen = self.taken_over_after_the_encode()
        progress = quiet()
        with patch:
            summary = convert_paths(TOOLS, [str(self.root)], convert_options(), progress)
        text = progress.stream.getvalue()
        self.assert_nothing_written_after_the_takeover(seen, summary, text, "was taken over by another run")
        self.assertTrue(vidfunnel.interrupt.draining.is_set())
        self.assertIn("lost the lock of folder '{}'".format(self.root), "".join(lines))

    def test_a_failed_encode_after_the_takeover_removes_and_saves_nothing(self):
        self.plan(REMUX_SOURCE)
        self.quiet_lost_lock()
        patch, seen = self.taken_over_after_the_encode(fail=True)
        progress = quiet()
        with patch:
            summary = convert_paths(TOOLS, [str(self.root)], convert_options(), progress)
        text = progress.stream.getvalue()
        self.assert_nothing_written_after_the_takeover(seen, summary, text, "was taken over by another run")
        self.assertEqual(summary.counts.get("failed"), 0, text)

    def test_a_lock_that_cannot_be_checked_after_the_encode_writes_nothing(self):
        self.plan(REMUX_SOURCE)
        self.quiet_lost_lock()
        patch, seen = self.taken_over_after_the_encode(silent_stat=True)
        real_stat = os.stat

        def unreachable(path, *rest, **values):
            if "unreachable" in seen and os.fspath(path) == seen["unreachable"]:
                raise OSError(errno.EIO, "Input/output error")
            return real_stat(path, *rest, **values)

        progress = quiet()
        with patch, unittest.mock.patch("vidfunnel.record.os.stat", side_effect=unreachable):
            summary = convert_paths(TOOLS, [str(self.root)], convert_options(), progress)
        text = progress.stream.getvalue()
        self.assert_nothing_written_after_the_takeover(seen, summary, text, "could not be checked")
        self.assertIn("Input/output error", text)

    def test_a_leftover_part_file_is_not_removed_once_the_lock_was_taken(self):
        """Host B took the folder and is writing this record's part file: the old holder leaves it alone."""
        record = self.plan(REMUX_SOURCE)[REMUX_SOURCE]
        leftover = part_path(self.root / "h264_high_mkv_aac.mp4", record["id"])
        leftover.write_bytes(b"PARTIAL OUTPUT OF HOST B, STILL BEING WRITTEN")
        record_bytes = record_path(self.root / REMUX_SOURCE).read_bytes()
        self.quiet_lost_lock()
        lock_file = self.root / ".vidfunnel" / "lock"
        real_change = encode.source_change

        def checked_then_host_b(*arguments, **keywords):
            result = real_change(*arguments, **keywords)
            os.unlink(str(lock_file))
            lock_file.write_text(json.dumps({"host": "hostb", "pid": 4242, "started": "2026-11-09T12:00:00Z",
                                             "heartbeat": {"interval": 300}}), encoding="utf-8")
            return result

        progress = quiet()
        with unittest.mock.patch("vidfunnel.encode.source_change", side_effect=checked_then_host_b), \
                unittest.mock.patch("vidfunnel.encode.run_tool", side_effect=AssertionError("ffmpeg started")):
            summary = convert_paths(TOOLS, [str(self.root)], convert_options(), progress)
        text = progress.stream.getvalue()
        self.assertEqual(leftover.read_bytes(), b"PARTIAL OUTPUT OF HOST B, STILL BEING WRITTEN", text)
        self.assertEqual(record_path(self.root / REMUX_SOURCE).read_bytes(), record_bytes)
        self.assertNotIn("removed a stale", text)
        self.assertEqual(summary.counts.get("skipped"), 1, text)
        self.assertIn("skipping 'h264_high_mkv_aac.mkv': the lock of folder '{}' was taken over by another "
                      "run".format(self.root), text)

    def test_a_held_lock_converts_as_before(self):
        self.plan(REMUX_SOURCE)
        progress = quiet()
        summary = convert_paths(TOOLS, [str(self.root)], convert_options(), progress)
        self.assertEqual(summary.counts.get("skipped"), 0, progress.stream.getvalue())
        self.assertTrue((self.root / "h264_high_mkv_aac.mp4").is_file())
        self.assertIn(load_record(self.root / REMUX_SOURCE)["status"], DONE)
        self.assertFalse(vidfunnel.interrupt.draining.is_set())

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

    def test_source_appended_after_plan_is_skipped_and_the_record_left_as_it_is(self):
        # Before 20261109.3 convert saved the status and flag 'source-changed' here.
        record = self.plan(REMUX_SOURCE)[REMUX_SOURCE]
        size = (self.root / REMUX_SOURCE).stat().st_size
        with open(str(self.root / REMUX_SOURCE), "ab") as handle:
            handle.write(b"appended after planning")
        record_bytes = record_path(self.root / REMUX_SOURCE).read_bytes()
        stage = self.convert(REMUX_SOURCE, record)
        self.assertEqual(stage["outcome"], "skipped")
        self.assertEqual(stage["skipped"],
                         "source changed since plan: '{}' differs from the fingerprint taken at plan (size {} bytes "
                         "recorded, {} now, first MiB differs, last MiB differs); plan it again with: vidfunnel plan "
                         "'{}'".format(REMUX_SOURCE, size, size + len(b"appended after planning"),
                                       self.root / REMUX_SOURCE))
        self.assertEqual(record_path(self.root / REMUX_SOURCE).read_bytes(), record_bytes)
        self.assertEqual(load_record(self.root / REMUX_SOURCE)["status"], "planned")
        self.assertFalse((self.root / "h264_high_mkv_aac.mp4").exists())

    def test_a_source_time_that_differs_alone_converts_with_ffmpeg(self):
        source = self.root / REMUX_SOURCE
        shutil.copy2(os.path.join(FIXTURE_DIR, REMUX_SOURCE), str(source))
        os.utime(str(source), ns=(PLANNED_MTIME_NS, PLANNED_MTIME_NS))
        plan_paths(TOOLS, [str(self.root)], make_options(), progress=quiet())
        record = load_record(source)
        self.assertEqual(record["source"]["fingerprint"]["mtime_ns"], PLANNED_MTIME_NS)
        whole_seconds = PLANNED_MTIME_NS - PLANNED_MTIME_NS % (1000 * 1000 * 1000)
        os.utime(str(source), ns=(whole_seconds, whole_seconds))
        stage = self.convert(REMUX_SOURCE, record)
        self.assertEqual(stage["outcome"], "ok", stage)
        self.assertEqual(stage["source_mtime_differs"],
                         [{"name": REMUX_SOURCE, "recorded": PLANNED_MTIME_NS, "seen": whole_seconds}])
        self.assertIs(stage["source_sha256_checked"], False)
        saved = load_record(source)
        self.assertIn(saved["status"], DONE)
        self.assertNotIn("source-changed", saved["flags"])
        output = self.root / "h264_high_mkv_aac.mp4"
        self.assertEqual(output.stat().st_mtime_ns, whole_seconds)
        self.assertEqual(saved["output"]["fingerprint"]["mtime_ns"], whole_seconds)

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

    def subtitle_record(self):
        if not os.path.exists(os.path.join(FIXTURE_DIR, SUBTITLE_SOURCE)):
            self.skipTest("fixture '{}' missing; run tests/make_fixtures.sh".format(SUBTITLE_SOURCE))
        return self.plan(SUBTITLE_SOURCE)[SUBTITLE_SOURCE]

    def test_an_older_record_without_a_text_sidecar_converts_without_one(self):
        # As planned before 2026-11-07: the subrip stream goes into the MP4 as mov_text, with no .srt listed.
        record = self.subtitle_record()
        for entry in record["stages"]["analyze"]["subtitles"]["result"]:
            entry.pop("sidecar", None)
        record["output"]["sidecars"] = []
        record["stages"]["plan"]["sidecars"] = []
        save_record(self.root / SUBTITLE_SOURCE, record)
        stage = self.convert(SUBTITLE_SOURCE, record)
        self.assertEqual(stage["outcome"], "ok", stage)
        self.assertEqual([name for name in os.listdir(str(self.root)) if name.endswith(".srt")], [])
        saved = load_record(self.root / SUBTITLE_SOURCE)
        self.assertIn(saved["status"], DONE)
        self.assertEqual(saved["stages"]["convert"]["sidecar_argvs"], [])
        self.assertEqual(saved["stages"]["convert"]["sidecars_written"], [])
        self.assertIn("mov_text", saved["stages"]["convert"]["argv"])

    def test_the_stored_sidecar_commands_are_never_run(self):
        record = self.subtitle_record()
        marker = Path(self.temporary.name) / "stored-sidecar-ran"
        script = Path(self.temporary.name) / "stored-sidecar-command"
        script.write_text("#!/bin/sh\necho ran > '{}'\nexit 0\n".format(marker))
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        stored = record["stages"]["plan"]["sidecars"]
        self.assertEqual(len(stored), 1, stored)
        stored[0]["argv"][0] = str(script)
        save_record(self.root / SUBTITLE_SOURCE, record)
        stage = self.convert(SUBTITLE_SOURCE, record)
        self.assertEqual(stage["outcome"], "ok", stage)
        self.assertFalse(marker.exists())
        self.assertIn("-->", (self.root / "h264_mkv_srt_subtitles.srt").read_text(errors="replace"))
        saved = load_record(self.root / SUBTITLE_SOURCE)
        self.assertEqual(saved["stages"]["convert"]["sidecars_written"], ["h264_mkv_srt_subtitles.srt"])
        self.assertEqual(saved["stages"]["convert"]["sidecar_argvs"][0][0], TOOLS.ffmpeg)

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
REFIT_ADVICE = ("the output 'h264_avi_mp3.mp4' was made from the earlier content; to convert the source as it is "
                "now, move 'h264_avi_mp3.mp4' away and run: vidfunnel plan '{}'")
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

    def test_a_refit_whose_lock_was_taken_during_the_encode_writes_nothing(self):
        self.plan_and_convert(REFIT_SOURCE, reencode_video=True)
        output = self.root / REFIT_OUTPUT
        output_bytes = output.read_bytes()
        self.addCleanup(vidfunnel.interrupt.draining.clear)
        self.addCleanup(vidfunnel.interrupt.drain_tally.reset)
        lock_file = self.root / ".vidfunnel" / "lock"
        seen = {}

        def encode_then_host_b(argv, *rest, **values):
            completed = ffmpeg_run(argv, *rest, **values)
            part = Path(argv[-1])
            os.unlink(str(lock_file))
            lock_file.write_text(json.dumps({"host": "hostb", "pid": 4242, "started": "2026-11-09T12:00:00Z",
                                             "heartbeat": {"interval": 300}}), encoding="utf-8")
            self.assertEqual(encode._clear_leftover(part), (True, None))
            part.write_bytes(b"PARTIAL REFIT OF HOST B")
            seen.update(part=part, record_bytes=record_path(self.root / REFIT_SOURCE).read_bytes())
            return completed

        with unittest.mock.patch("vidfunnel.encode.run_tool", side_effect=encode_then_host_b), \
                unittest.mock.patch("vidfunnel.record._warn"):
            summary, text = self.refit()
        self.assertEqual(output.read_bytes(), output_bytes, "the old holder replaced the output")
        self.assertEqual(seen["part"].read_bytes(), b"PARTIAL REFIT OF HOST B")
        self.assertEqual(record_path(self.root / REFIT_SOURCE).read_bytes(), seen["record_bytes"])
        self.assertEqual(summary.counts["refitted"] + summary.counts["kept"] + summary.counts["failed"], 0, text)
        self.assertIn("skipping '{}': the lock of folder '{}' was taken over by another run".format(
            REFIT_SOURCE, self.root), text)
        self.assertTrue(vidfunnel.interrupt.draining.is_set())

    def test_a_refit_leftover_part_file_is_not_removed_once_the_lock_was_taken(self):
        self.plan_and_convert(REFIT_SOURCE, reencode_video=True)
        output = self.root / REFIT_OUTPUT
        output_bytes = output.read_bytes()
        record_bytes = record_path(self.root / REFIT_SOURCE).read_bytes()
        leftover = part_path(output, load_record(self.root / REFIT_SOURCE)["id"])
        leftover.write_bytes(b"PARTIAL REFIT OF HOST B, STILL BEING WRITTEN")
        self.addCleanup(vidfunnel.interrupt.draining.clear)
        self.addCleanup(vidfunnel.interrupt.drain_tally.reset)
        lock_file = self.root / ".vidfunnel" / "lock"
        real_plan = encode.refit_plan

        def planned_then_host_b(*arguments, **keywords):
            result = real_plan(*arguments, **keywords)
            os.unlink(str(lock_file))
            lock_file.write_text(json.dumps({"host": "hostb", "pid": 4242, "started": "2026-11-09T12:00:00Z",
                                             "heartbeat": {"interval": 300}}), encoding="utf-8")
            return result

        with unittest.mock.patch("vidfunnel.encode.refit_plan", side_effect=planned_then_host_b), \
                unittest.mock.patch("vidfunnel.encode.run_tool", side_effect=AssertionError("ffmpeg started")), \
                unittest.mock.patch("vidfunnel.record._warn"):
            summary, text = self.refit()
        self.assertEqual(leftover.read_bytes(), b"PARTIAL REFIT OF HOST B, STILL BEING WRITTEN", text)
        self.assertEqual(output.read_bytes(), output_bytes)
        self.assertEqual(record_path(self.root / REFIT_SOURCE).read_bytes(), record_bytes)
        self.assertNotIn("removed a stale", text)
        self.assertIn("skipping '{}': the lock of folder '{}' was taken over by another run".format(
            REFIT_SOURCE, self.root), text)
        self.assertEqual(summary.counts["refitted"] + summary.counts["kept"] + summary.counts["failed"], 0, text)

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
        size = source.stat().st_size
        with open(str(source), "ab") as handle:
            handle.write(b"x")
        os.utime(str(source))
        record = load_record(source)
        record_bytes = record_path(source).read_bytes()
        output_hash = sha256_of(self.root / REFIT_OUTPUT)
        outcome, reason = refit_one(TOOLS, source, record, convert_options(), quiet())
        self.assertEqual((outcome, reason), (
            "skipped", "source changed since plan: '{}' differs from the fingerprint taken at plan (size {} bytes "
                       "recorded, {} now, first MiB differs, last MiB differs); {}".format(
                           REFIT_SOURCE, size, size + 1, REFIT_ADVICE.format(source))))
        self.assertEqual(record_path(source).read_bytes(), record_bytes)
        self.assertEqual(sha256_of(self.root / REFIT_OUTPUT), output_hash)

    def test_source_bytes_that_differ_at_the_same_size_are_skipped(self):
        self.plan_and_convert(REFIT_SOURCE, reencode_video=True)
        source = self.root / REFIT_SOURCE
        original = source.read_bytes()
        status = source.stat()
        output_hash = sha256_of(self.root / REFIT_OUTPUT)
        record_bytes = record_path(source).read_bytes()
        # The fixture is under 2 MiB, so its first and last MiB are the whole file and both differ.
        for change, edit in (("first byte", _flip_byte_at(0)), ("last byte", _flip_byte_at(-1))):
            with self.subTest(change=change):
                edit(source)
                os.utime(str(source), ns=(status.st_atime_ns, status.st_mtime_ns))
                outcome, reason = refit_one(TOOLS, source, load_record(source), convert_options(), quiet())
                self.assertEqual((outcome, reason), (
                    "skipped", "source changed since plan: '{}' differs from the fingerprint taken at plan "
                               "(first MiB differs, last MiB differs); {}".format(REFIT_SOURCE,
                                                                                  REFIT_ADVICE.format(source))))
                self.assertEqual(record_path(source).read_bytes(), record_bytes)
                self.assertEqual(sha256_of(self.root / REFIT_OUTPUT), output_hash)
                source.write_bytes(original)

    def large_source(self):
        """A converted re-encode whose source is then swapped for 3 MiB recorded at PLANNED_MTIME_NS; the record.

        The source check reads only the record's fingerprint and hash, so the
        swap leaves the rest of the record and the existing output valid.
        """
        record = self.plan_and_convert(REFIT_SOURCE, reencode_video=True)[REFIT_SOURCE]
        source = self.root / REFIT_SOURCE
        source.write_bytes(LARGE_CONTENT)
        os.utime(str(source), ns=(PLANNED_MTIME_NS, PLANNED_MTIME_NS))
        record["source"].update(fingerprint=cheap_fingerprint(source), sha256=sha256_of(source))
        save_record(source, record)
        return source, record

    def disk_check(self, record):
        with unittest.mock.patch("vidfunnel.record.full_sha256", wraps=record_module.full_sha256) as hasher:
            result = encode._refit_disk_problem(self.root, record, self.root / REFIT_OUTPUT, quiet=True)
        return result, hasher

    def test_source_times_are_checked_as_convert_checks_them(self):
        source, record = self.large_source()
        output = self.root / REFIT_OUTPUT
        output_ns = record["output"]["fingerprint"]["mtime_ns"]
        for word, recorded_ns, seen_ns in COARSER_VIEWS + list(OTHER_TIMES):
            with self.subTest(case=word):
                record["source"]["fingerprint"]["mtime_ns"] = recorded_ns
                os.utime(str(source), ns=(seen_ns, seen_ns))
                os.utime(str(output), ns=(output_ns + 1, output_ns + 1))
                (problem, _size, _source_size, notes), hasher = self.disk_check(record)
                read = (word, recorded_ns, seen_ns) in OTHER_TIMES
                self.assertIsNone(problem)
                self.assertEqual(hasher.call_count, 1 if read else 0)
                self.assertEqual(notes, {
                    "source_mtime_differs": [{"name": REFIT_SOURCE, "recorded": recorded_ns, "seen": seen_ns}],
                    "source_sha256_checked": read,
                    "output_mtime_differs": [{"name": REFIT_OUTPUT, "recorded": output_ns, "seen": output_ns + 1}]})

    def test_another_source_time_with_a_changed_middle_is_skipped_and_nothing_encoded(self):
        source, record = self.large_source()
        changed_middle(source)
        output_hash = sha256_of(self.root / REFIT_OUTPUT)
        record_bytes = record_path(source).read_bytes()
        for word, recorded_ns, seen_ns in OTHER_TIMES:
            with self.subTest(case=word):
                os.utime(str(source), ns=(seen_ns, seen_ns))
                with unittest.mock.patch("vidfunnel.encode.run_tool") as encoder:
                    outcome, reason = refit_one(TOOLS, source, load_record(source), convert_options(), quiet())
                encoder.assert_not_called()
                self.assertEqual((outcome, reason), (
                    "skipped", "source changed since plan: SHA-256 of '{}' is {}, the record says {}; {}".format(
                        REFIT_SOURCE, sha256_of(source), record["source"]["sha256"],
                        REFIT_ADVICE.format(source))))
                self.assertEqual(record_path(source).read_bytes(), record_bytes)
                self.assertEqual(sha256_of(self.root / REFIT_OUTPUT), output_hash)
                self.assertEqual(self.leftover_parts(), [])

    def test_times_that_differ_are_refitted_and_noted(self):
        self.plan_and_convert(REFIT_SOURCE, reencode_video=True)
        source = self.root / REFIT_SOURCE
        output = self.root / REFIT_OUTPUT
        record = load_record(source)
        source_ns = record["source"]["fingerprint"]["mtime_ns"]
        output_ns = record["output"]["fingerprint"]["mtime_ns"]
        days_later = source_ns + 3 * 86400 * SECOND_NS
        os.utime(str(source), ns=(days_later, days_later))
        os.utime(str(output), ns=(days_later + 1, days_later + 1))
        record_bytes = record_path(source).read_bytes()
        with unittest.mock.patch("vidfunnel.encode._verify", side_effect=lambda tools, key, record, *rest: record):
            outcome, reason = refit_one(TOOLS, source, record, convert_options(), quiet())
        self.assertEqual((outcome, reason), ("refitted", None))
        self.assertNotEqual(record_path(source).read_bytes(), record_bytes)
        saved = load_record(source)
        self.assertEqual(saved["status"], "converted")
        self.assertNotIn("source-changed", saved["flags"])
        self.assertEqual(saved["stages"]["refit"]["source_mtime_differs"],
                         [{"name": REFIT_SOURCE, "recorded": source_ns, "seen": days_later}])
        self.assertIs(saved["stages"]["refit"]["source_sha256_checked"], True)
        self.assertEqual(saved["stages"]["refit"]["output_mtime_differs"],
                         [{"name": REFIT_OUTPUT, "recorded": output_ns, "seen": days_later + 1}])

    def refit_with_a_coarser_source_time(self, **patches):
        """Refit, the source seen at whole seconds, with the given patches; (outcome, saved record, recorded, seen)."""
        self.plan_and_convert(REFIT_SOURCE, reencode_video=True)
        source = self.root / REFIT_SOURCE
        record = load_record(source)
        recorded_ns = PLANNED_MTIME_NS
        record["source"]["fingerprint"]["mtime_ns"] = recorded_ns
        save_record(source, record)
        whole_seconds = truncated(recorded_ns, SECOND_NS)
        os.utime(str(source), ns=(whole_seconds, whole_seconds))
        with contextlib.ExitStack() as stack:
            for target, value in patches.items():
                stack.enter_context(unittest.mock.patch(target, **value))
            outcome, _reason = refit_one(TOOLS, source, record, convert_options(), quiet())
        return outcome, load_record(source), recorded_ns, whole_seconds

    def assert_refit_note(self, saved, recorded_ns, seen_ns, outcome):
        stage = saved["stages"]["refit"]
        self.assertEqual(stage["outcome"], outcome)
        self.assertEqual(stage["source_mtime_differs"],
                         [{"name": REFIT_SOURCE, "recorded": recorded_ns, "seen": seen_ns}])
        self.assertIs(stage["source_sha256_checked"], not record_module.mtime_is_coarser_view(recorded_ns, seen_ns))

    def test_a_failed_refit_carries_the_note(self):
        outcome, saved, recorded_ns, seen_ns = self.refit_with_a_coarser_source_time(**{
            "vidfunnel.encode.run_tool": {"side_effect": FfmpegError("made-up ffmpeg failure", stderr="")}})
        self.assertEqual(outcome, "failed")
        self.assert_refit_note(saved, recorded_ns, seen_ns, "failed")

    def test_a_kept_refit_carries_the_note(self):
        outcome, saved, recorded_ns, seen_ns = self.refit_with_a_coarser_source_time(**{
            "vidfunnel.encode.refit_checks": {"return_value": "made-up check problem"}})
        self.assertEqual(outcome, "kept")
        self.assert_refit_note(saved, recorded_ns, seen_ns, "kept-existing")

    def test_a_refit_whose_replace_fails_carries_the_note(self):
        real_replace = os.replace
        output = str(self.root / REFIT_OUTPUT)

        def refuse_the_output(source, target, *rest, **options):
            if str(target) == output:
                raise OSError("made-up replace failure")
            return real_replace(source, target, *rest, **options)

        outcome, saved, recorded_ns, seen_ns = self.refit_with_a_coarser_source_time(**{
            "vidfunnel.encode.os.replace": {"side_effect": refuse_the_output}})
        self.assertEqual(outcome, "failed")
        self.assert_refit_note(saved, recorded_ns, seen_ns, "failed")
        self.assertIn("made-up replace failure", saved["stages"]["refit"]["error"])

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


# --- folder locks taken on demand (vidfunnel.turns) -------------------------------------------

POLL_DEADLINE_SECONDS = 60.0


def lock_file_of(folder):
    return Path(folder) / ".vidfunnel" / "lock"


def foreign_live_lock(folder):
    """A lock of another machine whose holder beat just now; another run must skip the folder."""
    import datetime
    path = lock_file_of(folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    started = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps({"host": "elsewhere-host", "pid": 4242, "started": started, "container": False,
                                "machine": "elsewhere-machine", "machine_source": "machine-id",
                                "boot": "elsewhere-boot", "flock": False,
                                "heartbeat": {"interval": record_module.HEARTBEAT_SECONDS}}), encoding="utf-8")
    return path


def wait_for(condition, deadline=POLL_DEADLINE_SECONDS):
    """Poll `condition` until it is true; False only when the generous deadline passes first."""
    import time
    ends = time.monotonic() + deadline
    while time.monotonic() < ends:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class LockFileSampler(object):
    """A thread that looks at the folders' lock files every millisecond and notes which folders it saw one in."""

    def __init__(self, folders):
        import threading
        self.folders = [Path(folder) for folder in folders]
        self.seen = set()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.watch, daemon=True)

    def watch(self):
        while not self.stop.is_set():
            self.seen.update(folder.name for folder in self.folders if lock_file_of(folder).exists())
            self.stop.wait(0.001)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_error):
        self.stop.set()
        self.thread.join(POLL_DEADLINE_SECONDS)
        return False


class OnDemandLockCase(unittest.TestCase):
    """convert and convert --refit over hand-written records, the item's work replaced by a stub; no ffmpeg."""

    def setUp(self):
        import threading
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
        self.guard = threading.Lock()
        self.events = []
        self.started_items = []
        self.during = {}          # source stem -> callable(key) run inside the stub, before its save
        self.item_delay = 0.0     # seconds slept at the start of each item, before its folder is entered
        self.acquire_delay = 0.0  # seconds slept inside each FolderLock.acquire

    def tally(self):
        import vidfunnel.interrupt
        tally = vidfunnel.interrupt.drain_tally
        return tally.seen, tally.finished, tally.left

    def write_record(self, folder_name, stem, kind="planned", output_name=None):
        folder = self.root / folder_name
        folder.mkdir(exist_ok=True)
        source = folder / (stem + ".avi")
        source.write_bytes(("source of " + stem + " ").encode() * 64)
        record = new_record(source)
        record["output"]["name"] = output_name or stem + ".mp4"
        if kind == "planned":
            record["action"] = "remux"
            set_status(record, "planned")
        elif kind == "finished":
            output = folder / record["output"]["name"]
            output.write_bytes(("output of " + stem + " ").encode() * 32)
            record["action"] = "remux"
            record["output"]["fingerprint"] = cheap_fingerprint(output)
            set_status(record, "converted")
        elif kind == "refit":
            output = folder / record["output"]["name"]
            output.write_bytes(("larger output of " + stem + " ").encode() * 96)
            record["action"] = "reencode"
            record["flags"] = ["output-larger"]
            record["output"]["fingerprint"] = cheap_fingerprint(output)
            set_status(record, "converted")
        save_record(source, record)
        return source

    def library(self, layout, kind="planned"):
        """layout: [(folder name, [stems])]; returns {stem: source path}."""
        return {stem: self.write_record(folder_name, stem, kind) for folder_name, stems in layout
                for stem in stems}

    def record_bytes(self, sources):
        return {stem: record_path(source).read_bytes() for stem, source in sources.items()}

    def log(self, *event):
        import threading
        with self.guard:
            self.events.append(event + (threading.get_ident(),))

    def held_here(self, folder):
        try:
            return json.loads(lock_file_of(folder).read_text(encoding="utf-8")).get("pid") == os.getpid()
        except (OSError, ValueError):
            return False

    def item_work(self, key, record, refit):
        """The stub's work: note the start and whether this run holds the folder, run `during`, save, note the end."""
        key = Path(key)
        folder = key.parent
        self.started_items.append(key.stem)
        self.log("start", folder.name, key.stem, self.held_here(folder))
        if key.stem in self.during:
            self.during[key.stem](key)
        try:
            encode.ensure_lock(folder)  # what the real item does before each write
        except record_module.LockLost as lost:
            self.log("end", folder.name, key.stem, False)
            return ("skipped", str(lost)) if refit else {"outcome": "skipped", "skipped": str(lost)}
        stage_name = "refit" if refit else "convert"
        record.setdefault("stages", {})[stage_name] = {"outcome": "stand-in"}
        set_status(record, "converted")
        save_record(key, record)
        self.log("end", folder.name, key.stem, True)
        return ("refitted", None) if refit else {"outcome": "ok"}

    def patches(self, refit):
        import threading
        import time
        case = self
        real_finished = encode._finished_record

        class LoggedLock(record_module.FolderLock):
            def acquire(self):
                case.log("acquiring", Path(self.folder).name)
                if case.acquire_delay:
                    time.sleep(case.acquire_delay)
                return super().acquire()

            def release(self):
                super().release()
                case.log("released", Path(self.folder).name)

        def finished(key, folder):
            if case.item_delay:
                time.sleep(case.item_delay)
            return real_finished(key, folder)

        def convert_stand_in(tools, key, record, options, progress=None, position=None, total=None):
            return case.item_work(key, record, refit=False)

        def refit_stand_in(tools, key, record, options, progress=None, position=None, total=None):
            return case.item_work(key, record, refit=True)

        return [unittest.mock.patch("vidfunnel.turns.FolderLock", LoggedLock),
                unittest.mock.patch("vidfunnel.encode._finished_record", side_effect=finished),
                unittest.mock.patch("vidfunnel.encode.convert_one", side_effect=convert_stand_in),
                unittest.mock.patch("vidfunnel.encode.refit_one", side_effect=refit_stand_in),
                unittest.mock.patch("vidfunnel.encode.run_tool", side_effect=AssertionError("ffmpeg started")),
                unittest.mock.patch("vidfunnel.record._warn")]

    def run_convert(self, jobs=1, refit=False):
        progress = quiet()
        with contextlib.ExitStack() as stack:
            for patch in self.patches(refit):
                stack.enter_context(patch)
            summary = convert_paths(None, [str(self.root)], convert_options(jobs=jobs, refit=refit), progress)
        return summary, progress.stream.getvalue()

    def acquired(self):
        return [event[1] for event in self.events if event[0] == "acquiring"]

    def assert_every_item_held_its_folder(self):
        starts = [event for event in self.events if event[0] == "start"]
        self.assertTrue(starts)
        self.assertEqual([event for event in starts if not event[3]], [], "an item ran without its folder's lock")

    def assert_released_after_its_last_item(self, folder_name):
        """Released once, after every item of the folder ended, by a thread whose last step was one of its items.

        Which item is the folder's last is decided when each item leaves, after the stand-in's 'end' is logged,
        so the release may come from a thread other than the one that logged the last 'end'; but the releasing
        thread does nothing between ending its own item of the folder and the release.
        """
        releases = [index for index, event in enumerate(self.events)
                    if event[0] == "released" and event[1] == folder_name]
        self.assertEqual(len(releases), 1, self.events)
        release = releases[0]
        ends = [index for index, event in enumerate(self.events) if event[0] == "end" and event[1] == folder_name]
        self.assertTrue(ends, folder_name)
        self.assertLess(ends[-1], release, "released before the folder's last item ended")
        thread = self.events[release][-1]
        before = [event for event in self.events[:release] if event[-1] == thread]
        self.assertTrue(before, self.events)
        self.assertEqual(before[-1][:2], ("end", folder_name), self.events)


class OnDemandConvertTest(OnDemandLockCase):

    THREE = [("first", ["harbour"]), ("second", ["meadow", "orchard"]), ("third", ["valley"])]

    def check_three_folders_without_jobs(self, refit):
        sources = self.library(self.THREE, kind="refit" if refit else "planned")
        seen = {}

        def look(key):
            seen[key.stem] = {name: lock_file_of(self.root / name).exists() for name in ("first", "second", "third")}

        self.during = {"meadow": look, "orchard": look}
        summary, text = self.run_convert(refit=refit)
        for stem in ("meadow", "orchard"):
            self.assertEqual(seen[stem], {"first": False, "second": True, "third": False}, text)
        for name in ("first", "second", "third"):
            self.assertFalse(lock_file_of(self.root / name).exists(), name)
        self.assertEqual(self.acquired(), ["first", "second", "third"])
        self.assert_every_item_held_its_folder()
        self.assertEqual(summary.counts["refitted" if refit else "converted"], 4, text)
        for stem in sources:
            self.assertEqual(load_record(sources[stem])["status"], "converted")

    def test_three_folders_without_jobs_hold_one_lock_at_a_time(self):
        self.check_three_folders_without_jobs(refit=False)

    def test_refit_three_folders_without_jobs_hold_one_lock_at_a_time(self):
        self.check_three_folders_without_jobs(refit=True)

    def check_two_jobs(self, refit):
        layout = [("first", ["harbour"]), ("second", ["meadow", "orchard", "prairie"]), ("third", ["valley"])]
        for item_delay, acquire_delay in ((0.0, 0.0), (0.05, 0.05)):
            with self.subTest(item_delay=item_delay, acquire_delay=acquire_delay):
                for name in ("first", "second", "third"):
                    shutil.rmtree(str(self.root / name), ignore_errors=True)
                self.events.clear()
                self.library(layout, kind="refit" if refit else "planned")
                self.item_delay, self.acquire_delay = item_delay, acquire_delay
                summary, text = self.run_convert(jobs=2, refit=refit)
                self.assertNotIn("locked by this run itself", text)
                self.assertNotIn("skipping folder", text)
                self.assertEqual(summary.counts["refitted" if refit else "converted"], 5, text)
                self.assertEqual(sorted(self.acquired()), ["first", "second", "third"])
                # Logged in one order under one lock, 'acquiring' before FolderLock.acquire creates the file and
                # 'released' after release() has removed it: each span covers its lock file's whole life, so
                # the most spans open at once bounds the lock files that existed at once. (Looking at the files
                # one after another cannot: a file seen early and another seen later need not have coexisted.)
                held = 0
                most = 0
                for event in self.events:
                    if event[0] == "acquiring":
                        held += 1
                        most = max(most, held)
                    elif event[0] == "released":
                        held -= 1
                self.assertLessEqual(most, 2, self.events)
                self.assert_every_item_held_its_folder()
                for name in ("first", "second", "third"):
                    self.assert_released_after_its_last_item(name)
                    self.assertFalse(lock_file_of(self.root / name).exists(), name)

    def test_two_jobs_hold_at_most_two_folders_and_free_each_after_its_last_item(self):
        self.check_two_jobs(refit=False)

    def test_refit_two_jobs_hold_at_most_two_folders_and_free_each_after_its_last_item(self):
        self.check_two_jobs(refit=True)

    def check_a_folder_held_elsewhere(self, refit):
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                for name in ("first", "second", "third"):
                    shutil.rmtree(str(self.root / name), ignore_errors=True)
                self.events.clear()
                self.started_items.clear()
                sources = self.library(self.THREE, kind="refit" if refit else "planned")
                foreign = foreign_live_lock(self.root / "second")
                foreign_bytes = foreign.read_bytes()
                held_records = self.record_bytes({stem: sources[stem] for stem in ("meadow", "orchard")})
                summary, text = self.run_convert(jobs=jobs, refit=refit)
                self.assertEqual(text.count("skipping folder '{}': ".format(self.root / "second")), 1, text)
                self.assertIn("elsewhere-host", text)
                self.assertEqual(self.record_bytes({stem: sources[stem] for stem in ("meadow", "orchard")}),
                                 held_records)
                self.assertEqual(foreign.read_bytes(), foreign_bytes)
                self.assertEqual(sorted(self.started_items), ["harbour", "valley"])
                if refit:
                    self.assertEqual((summary.counts["refitted"], summary.counts["skipped"]), (2, 2), text)
                    self.assertEqual(summary.skip_reasons, {"folder locked": 2})
                else:
                    self.assertEqual((summary.counts["converted"], summary.counts["skipped"]), (2, 2), text)
                self.assertEqual(summary.lock_errors, [])
                self.assertFalse(lock_file_of(self.root / "first").exists())
                self.assertFalse(lock_file_of(self.root / "third").exists())

    def test_a_folder_another_machine_holds_is_skipped_and_left_as_it_was(self):
        self.check_a_folder_held_elsewhere(refit=False)

    def test_refit_skips_a_folder_another_machine_holds(self):
        self.check_a_folder_held_elsewhere(refit=True)

    def test_a_finished_folder_is_never_locked_and_reports_as_before(self):
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                for name in ("summit", "valley"):
                    shutil.rmtree(str(self.root / name), ignore_errors=True)
                self.events.clear()
                finished = {stem: self.write_record("summit", stem, "finished") for stem in ("glacier", "ridge")}
                finished["lagoon"] = self.write_record("valley", "lagoon", "finished")
                planned = self.write_record("valley", "meadow")
                finished_bytes = self.record_bytes(finished)
                with LockFileSampler([self.root / "summit", self.root / "valley"]) as sampler:
                    summary, text = self.run_convert(jobs=jobs)
                self.assertNotIn("summit", self.acquired())
                self.assertNotIn("summit", sampler.seen, "the finished folder's lock file appeared")
                self.assertEqual(self.acquired(), ["valley"], "the mixed folder was not locked")
                for position, stem in ((1, "glacier"), (2, "ridge"), (3, "lagoon")):
                    self.assertIn("[convert {}/4] skipping '{}.avi': already converted\n".format(position, stem),
                                  text)
                self.assertEqual((summary.counts["skipped"], summary.counts["converted"]), (3, 1), text)
                self.assertEqual(self.record_bytes(finished), finished_bytes)
                self.assertEqual(load_record(planned)["status"], "converted")
                self.assert_every_item_held_its_folder()

    def test_output_names_are_judged_under_the_lock_on_the_records_as_they_are_then(self):
        real_shared = encode._shared_outputs
        judged = []

        def shared(runnable, stream):
            judged.append([(Path(folder).name, self.held_here(folder)) for _key, folder in runnable])
            return real_shared(runnable, stream)

        cases = (("made to meet after collection", "dusk.mp4", "dawn.mp4", 2),
                 ("parted after collection", "dawn.mp4", "dusk.mp4", 0))
        for words, collected_name, turn_name, collisions in cases:
            with self.subTest(words):
                for name in ("first", "second"):
                    shutil.rmtree(str(self.root / name), ignore_errors=True)
                judged.clear()
                self.started_items.clear()
                self.write_record("first", "harbour")
                dawn = self.write_record("second", "dawn")
                dusk = self.write_record("second", "dusk", output_name=collected_name)

                def rename_dusk(_key, name=turn_name):
                    record = load_record(dusk)
                    record["output"]["name"] = name
                    record_module._write_json_atomic(record_path(dusk), record)

                self.during = {"harbour": rename_dusk}
                with unittest.mock.patch("vidfunnel.encode._shared_outputs", side_effect=shared):
                    summary, text = self.run_convert()
                self.assertEqual(summary.counts.get("collision", 0), collisions, text)
                self.assertTrue(all(held for members in judged for _name, held in members), judged)
                if collisions:
                    self.assertEqual(self.started_items, ["harbour"])
                    self.assertIn("refusing 'dawn.avi' and 'dusk.avi' in '{}'".format(self.root / "second"), text)
                    self.assertEqual(load_record(dawn)["status"], "planned")
                else:
                    self.assertEqual(self.started_items, ["harbour", "dawn", "dusk"])
                    self.assertNotIn("refusing", text)

    def test_two_planned_records_with_one_output_name_are_refused_under_the_lock(self):
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                shutil.rmtree(str(self.root / "second"), ignore_errors=True)
                self.started_items.clear()
                sources = {"dawn": self.write_record("second", "dawn", output_name="evening.mp4"),
                           "dusk": self.write_record("second", "dusk", output_name="evening.mp4")}
                before = self.record_bytes(sources)
                summary, text = self.run_convert(jobs=jobs)
                self.assertEqual(summary.counts.get("collision"), 2, text)
                self.assertEqual(self.started_items, [])
                self.assertEqual(self.record_bytes(sources), before)

    def check_a_drain_during_the_first_folder(self, refit):
        import vidfunnel.interrupt
        layout = [("first", ["harbour", "jetty"]), ("second", ["meadow", "orchard"]), ("third", ["valley"])]
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                for name in ("first", "second", "third"):
                    shutil.rmtree(str(self.root / name), ignore_errors=True)
                vidfunnel.interrupt.draining.clear()
                vidfunnel.interrupt.drain_tally.reset()
                self.events.clear()
                self.started_items.clear()
                self.library(layout, kind="refit" if refit else "planned")
                # jetty, on the other worker with --jobs 2, ends only once the drain has begun, so no worker can
                # take the second folder's first item before it.
                self.during = {"harbour": lambda _key: vidfunnel.interrupt.draining.set(),
                               "jetty": lambda _key: self.assertTrue(
                                   wait_for(vidfunnel.interrupt.draining.is_set), "the drain never began")}
                summary, text = self.run_convert(jobs=jobs, refit=refit)
                self.assertEqual(self.acquired(), ["first"])
                self.assertFalse(any(lock_file_of(self.root / name).exists() for name in ("first", "second", "third")))
                seen, _finished, left = self.tally()
                self.assertTrue(seen)
                self.assertNotIn("meadow", self.started_items)
                self.assertEqual(left, 5 - len(self.started_items), self.started_items)
                self.assertEqual(summary.counts["refitted" if refit else "converted"], len(self.started_items), text)

    def test_a_drain_during_the_first_folder_locks_no_further_folder(self):
        self.check_a_drain_during_the_first_folder(refit=False)

    def test_refit_a_drain_during_the_first_folder_locks_no_further_folder(self):
        self.check_a_drain_during_the_first_folder(refit=True)

    def check_a_drain_as_an_item_starts(self, refit):
        """The drain begins after the item was handed out and before its folder is entered: no lock, counted left."""
        import vidfunnel.interrupt
        layout = [("first", ["harbour"]), ("second", ["meadow"]), ("third", ["valley"])]
        real_enter = vidfunnel.turns.FolderTurns.enter

        def enter(turns, folder):
            if Path(folder).name == "second":
                vidfunnel.interrupt.draining.set()
            return real_enter(turns, folder)

        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                for name in ("first", "second", "third"):
                    shutil.rmtree(str(self.root / name), ignore_errors=True)
                vidfunnel.interrupt.draining.clear()
                vidfunnel.interrupt.drain_tally.reset()
                self.events.clear()
                self.started_items.clear()
                self.library(layout, kind="refit" if refit else "planned")
                with unittest.mock.patch.object(vidfunnel.turns.FolderTurns, "enter", enter):
                    summary, text = self.run_convert(jobs=jobs, refit=refit)
                self.assertNotIn("second", self.acquired())
                self.assertNotIn("meadow", self.started_items)
                done = summary.counts["refitted" if refit else "converted"]
                seen, finished, left = self.tally()
                self.assertTrue(seen)
                self.assertEqual(done, len(self.started_items), text)
                self.assertEqual(left, 3 - len(self.started_items), (jobs, self.started_items, self.tally()))
                self.assertGreaterEqual(finished, 0)

    def test_a_drain_as_an_item_starts_takes_no_new_lock_and_counts_it_left(self):
        self.check_a_drain_as_an_item_starts(refit=False)

    def test_refit_a_drain_as_an_item_starts_takes_no_new_lock_and_counts_it_left(self):
        self.check_a_drain_as_an_item_starts(refit=True)

    def check_a_lost_lock_in_the_first_folder(self, refit):
        import vidfunnel.interrupt
        layout = [("first", ["harbour", "jetty"]), ("second", ["meadow"])]
        sources = self.library(layout, kind="refit" if refit else "planned")

        def taken_over(key):
            lock = lock_file_of(key.parent)
            os.unlink(str(lock))
            foreign_live_lock(key.parent)

        self.during = {"harbour": taken_over}
        jetty_bytes = record_path(sources["jetty"]).read_bytes()
        harbour_bytes = record_path(sources["harbour"]).read_bytes()
        summary, text = self.run_convert(refit=refit)
        self.assertTrue(vidfunnel.interrupt.draining.is_set())
        self.assertEqual(self.acquired(), ["first"])
        self.assertEqual(self.started_items, ["harbour"])
        self.assertEqual(record_path(sources["harbour"]).read_bytes(), harbour_bytes)
        self.assertEqual(record_path(sources["jetty"]).read_bytes(), jetty_bytes)
        self.assertIn("elsewhere-host", lock_file_of(self.root / "first").read_text(encoding="utf-8"),
                      "the new holder's lock was removed")
        self.assertFalse(lock_file_of(self.root / "second").exists())
        self.assertEqual(self.tally(), (True, 1, 2), "harbour was in progress when the drain began; jetty and meadow "
                         "never started")
        self.assertEqual(summary.counts["skipped"], 1, text)

    def test_a_lost_lock_in_the_first_folder_drains_and_locks_no_further_folder(self):
        self.check_a_lost_lock_in_the_first_folder(refit=False)

    def test_refit_a_lost_lock_in_the_first_folder_drains_and_locks_no_further_folder(self):
        self.check_a_lost_lock_in_the_first_folder(refit=True)

    def test_a_stop_file_in_the_second_folder_only_lets_the_first_complete(self):
        import vidfunnel.stopfile
        layout = [("first", ["harbour", "jetty"]), ("second", ["meadow", "orchard"]), ("third", ["valley"])]
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                for name in ("first", "second", "third"):
                    shutil.rmtree(str(self.root / name), ignore_errors=True)
                import vidfunnel.interrupt
                vidfunnel.interrupt.draining.clear()
                vidfunnel.interrupt.drain_tally.reset()
                vidfunnel.stopfile.reset()
                self.events.clear()
                self.started_items.clear()
                self.library(layout)
                record_module._write_json_atomic(vidfunnel.stopfile.stop_path(str(self.root / "second")),
                                                 {"host": "elsewhere", "requested": None, "mode": "drain"})
                with unittest.mock.patch("sys.stderr", io.StringIO()):
                    summary, text = self.run_convert(jobs=jobs)
                self.assertEqual(sorted(self.started_items), ["harbour", "jetty"])
                self.assertEqual(self.acquired(), ["first"])
                self.assertEqual(summary.counts["converted"], 2, text)
                self.assertEqual(self.tally()[2], 3)


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
