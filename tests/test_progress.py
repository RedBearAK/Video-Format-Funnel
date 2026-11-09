"""Tests for vidfunnel.progress: formatting, TTY and non-TTY rendering, ffmpeg adapter, summaries."""

VERSION = "20261109"

import io
import unittest

from vidfunnel.progress import (
    LINE_WIDTH,
    Progress,
    StageSummary,
    ffmpeg_progress_callback,
    format_bytes,
    format_duration,
    format_rate,
    is_tty,
    item_tag,
    say,
    tagged,
)


class FakeClock:
    """A clock that only moves when told to."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TerminalStream(io.StringIO):
    """A StringIO that claims to be a terminal."""

    def isatty(self):
        return True


class RecordingProgress:
    """Stands in for Progress and records each update call."""

    def __init__(self):
        self.calls = []

    def update(self, current=None, **fields):
        self.calls.append((current, fields))


class FormatTests(unittest.TestCase):

    def test_duration_boundaries(self):
        self.assertEqual(format_duration(0), "00:00:00")
        self.assertEqual(format_duration(59), "00:00:59")
        self.assertEqual(format_duration(59.9), "00:00:59")
        self.assertEqual(format_duration(60), "00:01:00")
        self.assertEqual(format_duration(3599), "00:59:59")
        self.assertEqual(format_duration(3600), "01:00:00")
        self.assertEqual(format_duration(90000), "25:00:00")
        self.assertEqual(format_duration(-5), "00:00:00")
        self.assertEqual(format_duration(None), "--:--:--")

    def test_bytes_boundaries(self):
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(1023), "1023 B")
        self.assertEqual(format_bytes(1024), "1.0 KiB")
        self.assertEqual(format_bytes(1024 ** 3), "1.0 GiB")
        self.assertEqual(format_bytes(int(1.2 * 1024 ** 3)), "1.2 GiB")
        self.assertEqual(format_bytes(85 * 1024 ** 2), "85 MiB")
        self.assertEqual(format_bytes(1024 ** 2 - 1), "1.0 MiB")

    def test_rate(self):
        self.assertEqual(format_rate(85 * 1024 ** 2), "85 MiB/s")
        self.assertEqual(format_rate(512), "512 B/s")
        self.assertEqual(format_rate(None), "-- B/s")


class StreamTests(unittest.TestCase):

    def test_is_tty(self):
        self.assertFalse(is_tty(io.StringIO()))
        self.assertTrue(is_tty(TerminalStream()))

    def test_say_writes_one_line(self):
        captured = io.StringIO()
        say("[plan 12/340] probing 'movie.avi'", captured)
        self.assertEqual(captured.getvalue(), "[plan 12/340] probing 'movie.avi'\n")


class TerminalRenderingTests(unittest.TestCase):

    def test_carriage_return_and_no_newline_until_finish(self):
        clock = FakeClock()
        terminal = TerminalStream()
        progress = Progress("re-encode", total=5530, unit="s", stream=terminal, clock=clock)
        progress.update(754, fps=212, speed=8.1, eta=588)
        clock.advance(0.5)
        progress.update(800, fps=212, speed=8.1, eta=580)
        output = terminal.getvalue()
        self.assertTrue(output.startswith("\r"))
        self.assertEqual(output.count("\r"), 2)
        self.assertNotIn("\n", output)
        self.assertIn("[re-encode] 00:12:34 / 01:32:10  13.6 %  212 fps  8.1x  eta 00:09:48", output)
        progress.finish()
        self.assertTrue(terminal.getvalue().endswith("\n"))
        self.assertEqual(terminal.getvalue().count("\n"), 1)

    def test_throttled_to_half_second(self):
        clock = FakeClock()
        terminal = TerminalStream()
        progress = Progress("hash", total=100, unit="items", stream=terminal, clock=clock)
        progress.update(1)
        clock.advance(0.2)
        progress.update(2)
        clock.advance(0.2)
        progress.update(3)
        self.assertEqual(terminal.getvalue().count("\r"), 1)
        clock.advance(0.1)
        progress.update(4)
        self.assertEqual(terminal.getvalue().count("\r"), 2)
        self.assertIn("4/100", terminal.getvalue())

    def test_finish_draws_latest_state_before_newline(self):
        clock = FakeClock()
        terminal = TerminalStream()
        with Progress("idet", total=12, unit="items", stream=terminal, clock=clock) as progress:
            progress.update(1)
            clock.advance(0.1)
            progress.update(12)
        self.assertTrue(terminal.getvalue().endswith("[idet] 12/12  100.0 %\n"))

    def test_shorter_line_is_padded_over_longer(self):
        clock = FakeClock()
        terminal = TerminalStream()
        progress = Progress("copy", total=10, unit="items", stream=terminal, clock=clock)
        progress.update(1, extra="a long piece of extra text")
        clock.advance(1)
        progress.update(2)
        last_draw = terminal.getvalue().split("\r")[-1]
        first_draw = terminal.getvalue().split("\r")[1]
        self.assertEqual(len(last_draw), len(first_draw))

    def test_output_is_ascii_without_escape_sequences(self):
        clock = FakeClock()
        terminal = TerminalStream()
        with Progress("verify", total=None, unit=None, stream=terminal, clock=clock) as progress:
            progress.update()
            clock.advance(3)
            progress.update()
        output = terminal.getvalue()
        self.assertNotIn("\x1b", output)
        output.encode("ascii")
        self.assertIn("elapsed 00:00:03", output)


class PlainRenderingTests(unittest.TestCase):

    def test_lines_only_every_thirty_seconds_and_always_the_last(self):
        clock = FakeClock()
        captured = io.StringIO()
        progress = Progress("re-encode", total=600, unit="s", stream=captured, clock=clock)
        for position in range(1, 30):
            clock.advance(1)
            progress.update(position)
        self.assertEqual(captured.getvalue(), "")
        clock.advance(1)
        progress.update(30)
        self.assertEqual(captured.getvalue().count("\n"), 1)
        self.assertNotIn("\r", captured.getvalue())
        self.assertIn("00:00:30 / 00:10:00", captured.getvalue())
        clock.advance(29)
        progress.update(59)
        self.assertEqual(captured.getvalue().count("\n"), 1)
        clock.advance(1)
        progress.update(60)
        self.assertEqual(captured.getvalue().count("\n"), 2)
        clock.advance(5)
        progress.update(65)
        progress.finish("re-encode done")
        lines = captured.getvalue().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertIn("00:01:05 / 00:10:00", lines[2])
        self.assertEqual(lines[3], "re-encode done")

    def test_last_state_not_repeated_when_already_written(self):
        clock = FakeClock()
        captured = io.StringIO()
        with Progress("copy", total=4, unit="items", stream=captured, clock=clock) as progress:
            clock.advance(30)
            progress.update(4)
        self.assertEqual(captured.getvalue().splitlines(), ["[copy] 4/4  100.0 %"])

    def test_bytes_line_shows_rate(self):
        clock = FakeClock()
        captured = io.StringIO()
        with Progress("sha256 'movie.avi'", total=1024 ** 3, unit="B", stream=captured,
                      clock=clock) as progress:
            clock.advance(2)
            progress.update(170 * 1024 ** 2)
        self.assertEqual(captured.getvalue(),
                         "[sha256 'movie.avi'] 170 MiB / 1.0 GiB  16.6 %  85 MiB/s\n")

    def test_quiet_writes_only_the_summary(self):
        clock = FakeClock()
        captured = io.StringIO()
        progress = Progress("copy", total=4, unit="items", stream=captured, clock=clock,
                            quiet=True)
        clock.advance(31)
        progress.update(2)
        progress.finish("copy: 4 copied")
        self.assertEqual(captured.getvalue(), "copy: 4 copied\n")

    def test_long_label_truncated_to_line_width(self):
        clock = FakeClock()
        captured = io.StringIO()
        with Progress("probing '" + "long name " * 30 + "'", total=1, unit="items",
                      stream=captured, clock=clock) as progress:
            progress.update(1)
        line = captured.getvalue().rstrip("\n")
        self.assertLessEqual(len(line), LINE_WIDTH)
        self.assertTrue(line.endswith("...] 1/1  100.0 %"))

    def test_unknown_unit_rejected(self):
        with self.assertRaises(ValueError):
            Progress("bad", unit="frames", stream=io.StringIO())


class FfmpegAdapterTests(unittest.TestCase):

    def test_eta_and_percent(self):
        recorder = RecordingProgress()
        callback = ffmpeg_progress_callback(recorder, 5530.0)
        callback({"out_time_s": 754.0, "fps": 212.0, "speed": 8.1, "frame": 18096})
        current, fields = recorder.calls[0]
        self.assertEqual(current, 754.0)
        self.assertEqual(fields["fps"], 212.0)
        self.assertEqual(fields["speed"], 8.1)
        self.assertAlmostEqual(fields["eta"], (5530.0 - 754.0) / 8.1)

    def test_eta_unknown_without_speed_or_total(self):
        recorder = RecordingProgress()
        ffmpeg_progress_callback(recorder, 100.0)({"out_time_s": 10.0, "speed": 0.0})
        ffmpeg_progress_callback(recorder, None)({"out_time_s": 10.0, "speed": 2.0})
        ffmpeg_progress_callback(recorder, 100.0)({"frame": 0})
        self.assertEqual([fields["eta"] for _, fields in recorder.calls], [None, None, None])

    def test_eta_never_negative(self):
        recorder = RecordingProgress()
        ffmpeg_progress_callback(recorder, 100.0)({"out_time_s": 101.0, "speed": 2.0})
        self.assertEqual(recorder.calls[0][1]["eta"], 0.0)

    def test_rendered_through_real_progress(self):
        clock = FakeClock()
        terminal = TerminalStream()
        progress = Progress("re-encode", total=5530.0, unit="s", stream=terminal, clock=clock)
        callback = ffmpeg_progress_callback(progress, 5530.0)
        callback({"out_time_s": 754.0, "fps": 212.0, "speed": 8.1, "frame": 18096})
        self.assertEqual(terminal.getvalue(),
                         "\r[re-encode] 00:12:34 / 01:32:10  13.6 %  212 fps  8.1x  eta 00:09:49")


class StageSummaryTests(unittest.TestCase):

    def test_format(self):
        clock = FakeClock()
        summary = StageSummary("plan", outcomes=("remux", "re-encode", "quarantine", "failed"),
                               clock=clock)
        for _ in range(12):
            summary.add("remux")
        summary.add("re-encode", 30)
        summary.add("quarantine", 2)
        clock.advance(252)
        self.assertEqual(summary.elapsed(), 252)
        self.assertEqual(
            summary.format(".vidfunnel/ under each folder"),
            "plan: 12 remux, 30 re-encode, 2 quarantine, 0 failed in 00:04:12;"
            " records in .vidfunnel/ under each folder")

    def test_format_without_location_and_unlisted_outcome(self):
        clock = FakeClock()
        summary = StageSummary("verify", outcomes=("ok",), clock=clock)
        summary.add("hold")
        self.assertEqual(summary.format(), "verify: 0 ok, 1 hold in 00:00:00")

    def test_format_empty(self):
        summary = StageSummary("retire", clock=FakeClock())
        self.assertEqual(summary.format(), "retire: no files in 00:00:00")


class AsciiStream(io.StringIO):
    """A StringIO whose write refuses characters outside ASCII, as a terminal in the C locale does."""

    encoding = "ascii"

    def write(self, text):
        text.encode("ascii")
        return io.StringIO.write(self, text)


class ItemTagTests(unittest.TestCase):
    """Every line of one item starts with its counter tag, so --jobs output says which item a line is from."""

    def test_item_tag_text(self):
        self.assertEqual(item_tag("convert", 3, 1054), "[convert 3/1054]")
        self.assertEqual(item_tag("refit", 1, 1), "[refit 1/1]")
        self.assertEqual(item_tag("verify"), "[verify]")
        self.assertEqual(item_tag("plan", 2, 0), "[plan]")

    def test_one_shot_lines_carry_the_tag(self):
        captured = io.StringIO()
        stream = tagged(captured, item_tag("convert", 3, 1054))
        say("re-encode 'movie.avi' -> 'movie.mp4'", stream)
        say("  converted 'movie.mp4'", stream)
        self.assertEqual(captured.getvalue(),
                         "[convert 3/1054] re-encode 'movie.avi' -> 'movie.mp4'\n"
                         "[convert 3/1054]   converted 'movie.mp4'\n")

    def test_plain_progress_lines_carry_the_tag(self):
        clock = FakeClock()
        captured = io.StringIO()
        stream = tagged(captured, item_tag("convert", 3, 1054))
        progress = Progress("re-encode", total=5828, unit="s", stream=stream, clock=clock)
        clock.advance(30)
        progress.update(4646)
        progress.finish()
        lines = captured.getvalue().splitlines()
        self.assertEqual(lines, ["[convert 3/1054] [re-encode] 01:17:26 / 01:37:08  79.7 %"])

    def test_terminal_redraws_keep_the_tag_after_each_carriage_return(self):
        clock = FakeClock()
        terminal = TerminalStream()
        stream = tagged(terminal, item_tag("refit", 2, 7))
        self.assertTrue(is_tty(stream))
        progress = Progress("refit", total=5828, unit="s", stream=stream, clock=clock)
        progress.update(4646)
        clock.advance(0.5)
        progress.update(4700)
        progress.finish("done")
        output = terminal.getvalue()
        self.assertEqual(output.count("\r"), 2)
        self.assertEqual(output.count("\r[refit 2/7] [refit] "), 2)
        self.assertTrue(output.startswith("\r[refit 2/7] [refit] 01:17:26 / 01:37:08  79.7 %"))
        self.assertTrue(output.endswith("\n[refit 2/7] done\n"), repr(output))

    def test_verify_progress_line_carries_the_tag(self):
        clock = FakeClock()
        captured = io.StringIO()
        progress = Progress("verify decode", total=600, unit="s", stream=tagged(captured, "[verify 4/9]"),
                            clock=clock)
        progress.update(300)
        progress.finish()
        self.assertEqual(captured.getvalue(), "[verify 4/9] [verify decode] 00:05:00 / 00:10:00  50.0 %\n")

    def test_two_items_on_one_line_stream_stay_apart(self):
        from vidfunnel.encode import _LineStream
        shared = io.StringIO()
        line_stream = _LineStream(shared)
        first = tagged(line_stream, item_tag("convert", 1, 2))
        second = tagged(line_stream, item_tag("convert", 2, 2))
        clock = FakeClock()
        one = Progress("re-encode", total=100, unit="s", stream=first, clock=clock)
        other = Progress("remux", total=100, unit="s", stream=second, clock=clock)
        for step in range(1, 4):
            clock.advance(30)
            one.update(10 * step)
            other.update(20 * step)
        one.finish()
        other.finish()
        lines = shared.getvalue().splitlines()
        self.assertEqual(len(lines), 6)
        for line in lines:
            if "[re-encode]" in line:
                self.assertTrue(line.startswith("[convert 1/2] [re-encode] "), line)
            else:
                self.assertTrue(line.startswith("[convert 2/2] [remux] "), line)

    def test_a_text_retried_after_an_encoding_error_keeps_its_tag(self):
        from vidfunnel.progress import _write
        captured = AsciiStream()
        stream = tagged(captured, "[plan 5/6]")
        # A write that does not end its line, as a progress line's text before its newline.
        _write(stream, "probing 'caf\u00e9.avi'")
        _write(stream, "\n")
        say("next", stream)
        self.assertEqual(captured.getvalue(), "[plan 5/6] probing 'caf\\xe9.avi'\n[plan 5/6] next\n")

    def test_empty_lines_and_split_writes(self):
        captured = io.StringIO()
        stream = tagged(captured, "[plan 1/1]")
        stream.write("first ")
        stream.write("half\n\nsecond\n")
        self.assertEqual(captured.getvalue(), "[plan 1/1] first half\n\n[plan 1/1] second\n")


if __name__ == "__main__":
    unittest.main()

# End of File #
