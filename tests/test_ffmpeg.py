"""Tests for vidfunnel.ffmpeg: pure parsers without ffmpeg, the runner and decode check with it.

Integration tests skip with a message when '/usr/bin/ffmpeg' is absent; the
distrobox wrapper test skips when the wrapper is not installed.
"""

VERSION = "20261102"

import os
import shutil
import stat
import tempfile
import unittest

from vidfunnel.ffmpeg import (
    FfmpegError,
    ToolNotFound,
    Tools,
    capabilities,
    classify_messages,
    ffprobe_json,
    ffprobe_packets,
    find_tools,
    normalise_message,
    null_decode,
    parse_progress_block,
    parse_version,
    run,
)
from vidfunnel.ffmpeg import _parse_bsfs, _parse_filters, _parse_table

HOST_FFMPEG = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"  # Homebrew puts it under /opt/homebrew
WRAPPER_FFMPEG = os.path.expanduser("~/.local/share/vidfunnel-tools/bin/ffmpeg")
NO_HOST_FFMPEG = "'{}' is absent; integration test skipped".format(HOST_FFMPEG)
NO_WRAPPER = "distrobox wrapper '{}' is absent; skipped".format(WRAPPER_FFMPEG)


def write_stub(path):
    with open(path, "w") as handle:
        handle.write("#!/bin/sh\nexit 0\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class ProgressBlockTests(unittest.TestCase):
    def test_typical_block(self):
        lines = [
            "frame=212\n",
            "fps=212.40\n",
            "stream_0_0_q=28.0\n",
            "bitrate=1234.5kbits/s\n",
            "total_size=1048576\n",
            "out_time_us=754000000\n",
            "out_time_ms=754000000\n",
            "out_time=00:12:34.000000\n",
            "dup_frames=0\n",
            "drop_frames=0\n",
            "speed=8.1x\n",
            "progress=continue\n",
        ]
        block = parse_progress_block(lines)
        self.assertEqual(block["frame"], 212)
        self.assertAlmostEqual(block["fps"], 212.4)
        self.assertAlmostEqual(block["speed"], 8.1)
        self.assertEqual(block["total_size"], 1048576)
        self.assertAlmostEqual(block["out_time_s"], 754.0)
        self.assertEqual(block["progress"], "continue")
        self.assertEqual(block["bitrate"], "1234.5kbits/s")

    def test_out_time_ms_is_microseconds_like_ffmpeg_writes_it(self):
        block = parse_progress_block(["out_time_ms=2500000", "progress=continue"])
        self.assertAlmostEqual(block["out_time_s"], 2.5)

    def test_clock_fallback(self):
        block = parse_progress_block(["out_time=01:02:03.500000", "progress=end"])
        self.assertAlmostEqual(block["out_time_s"], 3723.5)

    def test_not_available_values_become_none(self):
        block = parse_progress_block(
            ["frame=0", "fps=N/A", "speed=N/A", "total_size=N/A", "out_time_us=N/A", "progress=continue"]
        )
        self.assertIsNone(block["fps"])
        self.assertIsNone(block["speed"])
        self.assertIsNone(block["total_size"])
        self.assertIsNone(block["out_time_s"])
        self.assertEqual(block["frame"], 0)

    def test_negative_start_time_is_unknown(self):
        block = parse_progress_block(["out_time_us=-9223372036854775807", "progress=continue"])
        self.assertIsNone(block["out_time_s"])

    def test_blank_and_malformed_lines_are_skipped(self):
        block = parse_progress_block(["", "garbage without equals", "frame=3", "progress=end"])
        self.assertEqual(block["frame"], 3)
        self.assertNotIn("garbage without equals", block)


class NormaliseMessageTests(unittest.TestCase):
    def test_contract_example(self):
        self.assertEqual(
            normalise_message("[h264 @ 0x55a] error while decoding MB 32 20"),
            "[h264] error while decoding MB # #",
        )

    def test_codec_names_with_digits_survive(self):
        self.assertEqual(
            normalise_message("[mpeg4 @ 0x7f00aa] ac-tex damaged at 12 7"),
            "[mpeg4] ac-tex damaged at # #",
        )

    def test_stream_tags_and_hex_and_timestamps(self):
        first = normalise_message("[vist#0:0/h264 @ 0x55d1] corrupt decoded frame at 0x1f pts 12.345")
        second = normalise_message("[vist#0:0/h264 @ 0x6612] corrupt decoded frame at 0xab pts 99.1")
        self.assertEqual(first, second)
        self.assertEqual(first, "[vist##:#/h264] corrupt decoded frame at # pts #.#")

    def test_whitespace_collapsed(self):
        self.assertEqual(normalise_message("  [aac @ 0x1]   Input   buffer  \n"), "[aac] Input buffer")

    def test_untagged_line(self):
        self.assertEqual(normalise_message("Error while decoding stream #0:1"), "Error while decoding stream ##:#")

    def test_classify_counts_and_repeats(self):
        stderr = (
            "[h264 @ 0x1] error while decoding MB 1 2\n"
            "[h264 @ 0x2] error while decoding MB 3 4\n"
            "    Last message repeated 5 times\n"
            "[aac @ 0x3] Input buffer exhausted before END element found\n"
            "\n"
        )
        self.assertEqual(
            classify_messages(stderr),
            {
                "[h264] error while decoding MB # #": 7,
                "[aac] Input buffer exhausted before END element found": 1,
            },
        )


class ParseVersionTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_version("ffmpeg version 7.1.5 Copyright (c) 2000-2026"), (7, 1, 5))
        self.assertEqual(parse_version("n9.0.2"), (9, 0, 2))
        self.assertEqual(parse_version("7.1.5-0+deb13u1"), (7, 1, 5))
        self.assertEqual(parse_version("8.1"), (8, 1))
        self.assertEqual(parse_version("ffprobe version 8.1.3 Copyright"), (8, 1, 3))

    def test_git_build_and_garbage(self):
        self.assertEqual(parse_version("N-112345-gabcdef"), (0,))
        self.assertEqual(parse_version("ffmpeg version N-112345-gabcdef Copyright"), (0,))
        self.assertEqual(parse_version(""), (0,))


class ListingParserTests(unittest.TestCase):
    def test_format_listing_with_device_flags_and_aliases(self):
        listing = (
            "Formats:\n D.. = Demuxing supported\n .E. = Muxing supported\n ..d = Is a device\n ---\n"
            " D d alsa            ALSA audio input\n"
            " D   dvdvideo        DVD-Video\n"
            " D   mov,mp4,m4a,3gp,3g2,mj2 QuickTime / MOV\n"
        )
        self.assertEqual(
            _parse_table(listing, split_commas=True),
            {"alsa", "dvdvideo", "mov", "mp4", "m4a", "3gp", "3g2", "mj2"},
        )

    def test_codec_listing(self):
        listing = (
            "Encoders:\n V..... = Video\n ------\n"
            " V....D libx264              libx264 H.264 / AVC\n"
            " A....D aac                  AAC (Advanced Audio Coding)\n"
        )
        self.assertEqual(_parse_table(listing, split_commas=False), {"libx264", "aac"})

    def test_filter_listing_ffmpeg_seven_and_eight(self):
        seven = "Filters:\n  T.. = Timeline support\n  A = Audio input/output\n TSC bwdif  V->V  Deinterlace.\n"
        eight = "Filters:\n  T.. = Timeline support\n  ------\n T. cropdetect  V->V  Auto-detect.\n .. nullsrc |->V x\n"
        self.assertEqual(_parse_filters(seven), {"bwdif"})
        self.assertEqual(_parse_filters(eight), {"cropdetect", "nullsrc"})

    def test_bsf_listing(self):
        self.assertEqual(_parse_bsfs("Bitstream filters:\naac_adtstoasc\nh264_metadata\n\n"),
                         {"aac_adtstoasc", "h264_metadata"})


class FindToolsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="vidfunnel-find-")
        self.addCleanup(shutil.rmtree, self.folder)

    def test_ffprobe_next_to_ffmpeg(self):
        ffmpeg = os.path.join(self.folder, "ffmpeg")
        ffprobe = os.path.join(self.folder, "ffprobe")
        write_stub(ffmpeg)
        write_stub(ffprobe)
        self.assertEqual(find_tools(ffmpeg), Tools(ffmpeg, ffprobe))

    def test_name_pattern_is_followed(self):
        ffmpeg = os.path.join(self.folder, "ffmpeg-seven")
        ffprobe = os.path.join(self.folder, "ffprobe-seven")
        write_stub(ffmpeg)
        write_stub(ffprobe)
        self.assertEqual(find_tools(ffmpeg), Tools(ffmpeg, ffprobe))

    def test_falls_back_to_path_when_no_sibling(self):
        ffmpeg = os.path.join(self.folder, "ffmpeg")
        write_stub(ffmpeg)
        on_path = shutil.which("ffprobe")
        if on_path is None:
            with self.assertRaises(ToolNotFound) as caught:
                find_tools(ffmpeg)
            self.assertIn(ffmpeg, str(caught.exception))
        else:
            self.assertEqual(find_tools(ffmpeg).ffprobe, os.path.abspath(on_path))

    def test_missing_ffmpeg_names_the_path(self):
        missing = os.path.join(self.folder, "nowhere", "ffmpeg")
        with self.assertRaises(ToolNotFound) as caught:
            find_tools(missing)
        self.assertIn("'{}'".format(missing), str(caught.exception))

    def test_non_executable_ffmpeg_is_refused(self):
        ffmpeg = os.path.join(self.folder, "ffmpeg")
        with open(ffmpeg, "w") as handle:
            handle.write("not a program\n")
        with self.assertRaises(ToolNotFound):
            find_tools(ffmpeg)


class RunnerTests(unittest.TestCase):
    def test_missing_executable_raises_tool_not_found(self):
        with self.assertRaises(ToolNotFound):
            run(["/nonexistent/vidfunnel-test-program", "-version"])

    def test_failure_raises_with_stderr(self):
        script = "import sys; sys.stderr.write('first\\nsecond failure\\n'); sys.exit(4)"
        with self.assertRaises(FfmpegError) as caught:
            run([shutil.which("python3") or "python3", "-c", script])
        self.assertEqual(caught.exception.returncode, 4)
        self.assertIn("second failure", caught.exception.stderr)
        self.assertIn("second failure", str(caught.exception))

    def test_progress_blocks_and_large_stderr_do_not_deadlock(self):
        # 1 MiB of stderr would fill the pipe and hang a reader that only drains stdout.
        script = (
            "import sys\n"
            "sys.stderr.write('x' * 1048576 + '\\n'); sys.stderr.flush()\n"
            "for number in range(3):\n"
            "    print('frame=%d' % number); print('speed=2.0x')\n"
            "    print('progress=%s' % ('end' if number == 2 else 'continue'), flush=True)\n"
        )
        blocks = []
        completed = run([shutil.which("python3") or "python3", "-c", script], progress=blocks.append)
        self.assertEqual([block["frame"] for block in blocks], [0, 1, 2])
        self.assertEqual(blocks[-1]["progress"], "end")
        self.assertEqual(len(completed.stderr), 1048577)

    def test_callback_exception_kills_the_process(self):
        script = (
            "import time\n"
            "print('progress=continue', flush=True)\n"
            "time.sleep(30)\n"
        )

        def explode(block):
            raise RuntimeError("stop here")

        with self.assertRaises(RuntimeError):
            run([shutil.which("python3") or "python3", "-c", script], progress=explode)


@unittest.skipUnless(os.path.exists(HOST_FFMPEG), NO_HOST_FFMPEG)
class HostFfmpegTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.mkdtemp(prefix="vidfunnel-ffmpeg-")
        cls.tools = find_tools(HOST_FFMPEG)
        cls.clip = os.path.join(cls.folder, "testclip.mkv")
        cls.blocks = []
        argv = [
            cls.tools.ffmpeg, "-hide_banner", "-v", "error", "-nostats", "-progress", "pipe:1", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", cls.clip,
        ]
        cls.encode = run(argv, progress=cls.blocks.append)
        cls.damaged = os.path.join(cls.folder, "damaged clip.mkv")
        shutil.copyfile(cls.clip, cls.damaged)
        size = os.path.getsize(cls.damaged)
        with open(cls.damaged, "r+b") as handle:
            for fraction in (0.4, 0.5, 0.6):
                handle.seek(int(size * fraction))
                handle.write(bytes((position * 37 + 11) % 256 for position in range(64)))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.folder)

    def test_capabilities_of_host_build(self):
        found = capabilities(self.tools)
        self.assertGreaterEqual(found["major"], 6)
        self.assertEqual(found["version_tuple"][0], found["major"])
        self.assertIn("libx264", found["encoders"])
        self.assertIn("aac", found["encoders"])
        self.assertIn("h264", found["decoders"])
        self.assertIn("mov", found["demuxers"])
        self.assertIn("mp4", found["muxers"])
        self.assertTrue({"idet", "bwdif", "fieldmatch", "cropdetect"} <= found["filters"])
        self.assertIn("h264_metadata", found["bsfs"])
        self.assertIn("--enable-libx264", found["configuration"])
        self.assertIs(capabilities(self.tools), found)

    def test_run_reports_progress_blocks(self):
        self.assertEqual(self.encode.returncode, 0)
        self.assertTrue(self.blocks)
        last = self.blocks[-1]
        self.assertEqual(last["progress"], "end")
        self.assertGreater(last["frame"], 0)
        self.assertAlmostEqual(last["out_time_s"], 3.0, delta=0.2)

    def test_ffprobe_json(self):
        data = ffprobe_json(self.tools, self.clip)
        kinds = sorted(stream["codec_type"] for stream in data["streams"])
        self.assertEqual(kinds, ["audio", "video"])
        self.assertAlmostEqual(float(data["format"]["duration"]), 3.0, delta=0.2)
        self.assertIn("chapters", data)

    def test_ffprobe_json_on_damaged_clip_still_reads(self):
        data = ffprobe_json(self.tools, self.damaged)
        self.assertEqual(len(data["streams"]), 2)

    def test_ffprobe_json_unreadable_file_raises(self):
        junk = os.path.join(self.folder, "not video.avi")
        with open(junk, "wb") as handle:
            handle.write(b"\0" * 64)
        with self.assertRaises(FfmpegError) as caught:
            ffprobe_json(self.tools, junk)
        self.assertIn("'{}'".format(junk), str(caught.exception))

    def test_ffprobe_packets_keyframes(self):
        packets = ffprobe_packets(self.tools, self.clip, "v:0", "pts_time,flags")
        self.assertEqual(len(packets), 75)
        self.assertIn("K", packets[0]["flags"])

    def test_clean_decode_has_no_messages(self):
        result = null_decode(self.tools, self.clip)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.message_classes, {})

    def test_damaged_decode_reports_message_classes(self):
        blocks = []
        result = null_decode(self.tools, self.damaged, progress=blocks.append)
        self.assertTrue(result.message_classes, "no messages from a damaged clip:\n" + result.stderr)
        self.assertTrue(any(name.startswith("[h264]") for name in result.message_classes), result.message_classes)
        for name in result.message_classes:
            self.assertNotIn("0x", name)
        self.assertEqual(blocks[-1]["progress"], "end")

    def test_relative_name_with_leading_dash(self):
        dashed = os.path.join(self.folder, "-dash name.mkv")
        shutil.copyfile(self.clip, dashed)
        previous = os.getcwd()
        os.chdir(self.folder)
        try:
            data = ffprobe_json(self.tools, "-dash name.mkv")
        finally:
            os.chdir(previous)
        self.assertEqual(len(data["streams"]), 2)


@unittest.skipUnless(os.path.exists(WRAPPER_FFMPEG), NO_WRAPPER)
@unittest.skipUnless(os.path.exists(HOST_FFMPEG), NO_HOST_FFMPEG)
class DistroboxWrapperTests(unittest.TestCase):
    def test_wrapper_has_dvdvideo_and_host_does_not(self):
        wrapper_tools = find_tools(WRAPPER_FFMPEG)
        self.assertEqual(wrapper_tools.ffmpeg, WRAPPER_FFMPEG)
        wrapper_found = capabilities(wrapper_tools)
        host_found = capabilities(find_tools(HOST_FFMPEG))
        self.assertIn("dvdvideo", wrapper_found["demuxers"])
        self.assertNotIn("dvdvideo", host_found["demuxers"])
        self.assertIn("libx264", wrapper_found["encoders"])
        self.assertIn("bwdif", wrapper_found["filters"])


if __name__ == "__main__":
    unittest.main()

# End of File #
