"""Terminal feedback for every stage: start lines, live progress, stage summaries.

HANDOFF.md section 6.10. Nothing that may take more than a second runs
silently. On a TTY a live progress line is overwritten in place with a
carriage return; when the stream is not a TTY (log file, CI, interleaved
jobs) the same text is written as a new line at most every 30 seconds, and
the final state is always written. Output is plain ASCII produced by this
module (caller text such as file names is passed through unchanged), with no
colour and no ANSI sequences: the only control character is '\\r'.

The clock and the output stream are injectable so that tests can drive the
throttling deterministically.
"""

VERSION = "20261108"

import errno
import os
import sys
import time

LINE_WIDTH = 100
UNITS = ("s", "B", "items", None)

_BYTE_PREFIXES = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")


def _resolve_stream(stream):
    """Return the stream to write to: the given one, or sys.stdout looked up now."""
    if stream is None:
        return sys.stdout
    return stream


def is_tty(stream=None):
    """True when the stream (default sys.stdout) is an interactive terminal."""
    target = _resolve_stream(stream)
    isatty = getattr(target, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except (ValueError, OSError):
        # A closed or detached stream is not a terminal.
        return False


# A write that fails with one of these finds its reader gone: a closed terminal (EIO) or a pipe whose reader
# has exited (EPIPE), as after a hangup, or Ctrl+C or Ctrl+Backslash to a run piped through tee.
_GONE_ERRNOS = (errno.EIO, errno.EPIPE)


def _write(stream, text):
    """Write text and flush. Characters the stream cannot encode are escaped, not dropped.

    When the reader is gone (_GONE_ERRNOS), the text is dropped and the
    stream's file descriptor is pointed at /dev/null (discard_output), so
    neither this nor any later line, nor the flush at exit, fails: the
    output is lost with its reader, while the records, which are the
    authority, are written as usual and no item is failed for a write.
    """
    try:
        try:
            stream.write(text)
        except UnicodeEncodeError:
            encoding = getattr(stream, "encoding", None) or "ascii"
            stream.write(text.encode(encoding, "backslashreplace").decode(encoding))
        flush = getattr(stream, "flush", None)
        if flush is not None:
            flush()
    except OSError as error:
        if error.errno not in _GONE_ERRNOS:
            raise
        discard_output(stream)


def discard_output(stream):
    """Point the file descriptor behind `stream` at /dev/null; a stream without one is left as it is.

    os.dup2 replaces the descriptor in place, so every writer of it (both
    sys.stdout and a wrapper around it) now writes to /dev/null, and the
    bytes still in the stream's buffer go there at the next flush.
    """
    try:
        target = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null, target)
    finally:
        os.close(null)


def say(message, stream=None):
    """Write one plain line, immediately flushed: "[plan 12/340] probing 'x.avi'"."""
    _write(_resolve_stream(stream), "{}\n".format(message))


def format_duration(seconds):
    """Seconds as 'HH:MM:SS' (whole seconds, truncated; hours grow past 99).

    None renders as '--:--:--'; negative values render as '00:00:00'.
    """
    if seconds is None:
        return "--:--:--"
    total = int(seconds)
    if total < 0:
        total = 0
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return "{:02d}:{:02d}:{:02d}".format(hours, minutes, secs)


def _scaled(value):
    """Scale a non-negative byte value to binary prefixes: (number text, prefix)."""
    if value < 1024:
        return "{:d}".format(int(value)), _BYTE_PREFIXES[0]
    scaled = float(value)
    index = 0
    while scaled >= 1024 and index < len(_BYTE_PREFIXES) - 1:
        scaled /= 1024.0
        index += 1
    # Below 10 one decimal, otherwise whole numbers; promote when rounding reaches 1024.
    text = "{:.1f}".format(scaled) if scaled < 9.95 else "{:.0f}".format(scaled)
    if float(text) >= 1024 and index < len(_BYTE_PREFIXES) - 1:
        scaled /= 1024.0
        index += 1
        text = "{:.1f}".format(scaled)
    return text, _BYTE_PREFIXES[index]


def format_bytes(count):
    """Byte count with binary prefixes: '1023 B', '1.0 KiB', '85 MiB', '1.2 GiB'."""
    if count is None:
        return "-- B"
    number, prefix = _scaled(max(0, count))
    return "{} {}".format(number, prefix)


def format_rate(bytes_per_second):
    """Byte rate with binary prefixes: '85 MiB/s', '1.2 GiB/s'."""
    if bytes_per_second is None:
        return "-- B/s"
    number, prefix = _scaled(max(0, bytes_per_second))
    return "{} {}/s".format(number, prefix)


def _format_amount(value, unit):
    """One measured value in the line's unit."""
    if unit == "s":
        return format_duration(value)
    if unit == "B":
        return format_bytes(value)
    if value is None:
        return "--"
    if isinstance(value, float) and not value.is_integer():
        return "{:.1f}".format(value)
    return "{:d}".format(int(value))


def _fit_line(label, rest, width=LINE_WIDTH):
    """'[label] rest' within width characters, shortening the label first."""
    line = "[{}] {}".format(label, rest) if rest else "[{}]".format(label)
    if len(line) <= width:
        return line
    room = width - len(rest) - 3 - (1 if not rest else 0)
    if room >= 4:
        short_label = label[: room - 3] + "..."
        return "[{}] {}".format(short_label, rest) if rest else "[{}]".format(short_label)
    return line[:width]


class Progress:
    """One live progress line for a long operation.

    Progress(label, total=None, unit='s', stream=None, interval_tty=0.5,
             interval_plain=30.0, clock=time.monotonic, quiet=False)

    unit is 's' (current and total are seconds of media), 'B' (bytes; a rate
    is shown from elapsed time), 'items' (a counter such as 'window 7/12') or
    None (elapsed time only). With total None the line shows the elapsed time
    and, when given, the current value; never a spinner.

    update(current, fps=None, speed=None, eta=None, extra=None) records the
    state and renders it when the interval has passed. finish(summary=None)
    ends the line (newline on a TTY, the final state on a non-TTY) and writes
    the summary line if given. Used as a context manager, finish() runs on
    exit, also when an exception escapes, so an error message never lands on
    the end of a half-drawn line.

    quiet=True suppresses the live lines (the --quiet option); a summary
    passed to finish() is still written.
    """

    def __init__(self, label, total=None, unit="s", stream=None, interval_tty=0.5,
                 interval_plain=30.0, clock=time.monotonic, quiet=False):
        if unit not in UNITS:
            raise ValueError("unknown progress unit '{}', expected one of {}".format(
                unit, ", ".join(repr(name) for name in UNITS)))
        self.label = label
        self.total = total
        self.unit = unit
        self.stream = _resolve_stream(stream)
        self.tty = is_tty(self.stream)
        self.interval = interval_tty if self.tty else interval_plain
        self.clock = clock
        self.quiet = quiet
        self.started = clock()
        self.current = None
        self.fields = {}
        self.finished = False
        self._have_update = False
        self._last_render_at = None
        self._last_rendered_text = None
        self._drawn_width = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if not self.finished:
            self.finish()
        return False

    def elapsed(self):
        """Seconds since the Progress was created, on its clock."""
        return max(0.0, self.clock() - self.started)

    def percent(self):
        """Percentage of total done (0 to 100), or None when it cannot be known."""
        if self.total is None or self.current is None or self.total <= 0:
            return None
        return min(100.0, max(0.0, 100.0 * self.current / self.total))

    def update(self, current=None, fps=None, speed=None, eta=None, extra=None):
        """Record the state and render it if the interval since the last render has passed."""
        if self.finished:
            return
        self.current = current
        self.fields = {"fps": fps, "speed": speed, "eta": eta, "extra": extra}
        self._have_update = True
        now = self.clock()
        if self.tty:
            due = (self._last_render_at is None
                   or now - self._last_render_at >= self.interval)
        else:
            reference = self.started if self._last_render_at is None else self._last_render_at
            due = now - reference >= self.interval
        if due:
            self._render(now)

    def render_text(self):
        """The current line's text, without '\\r' or newline, at most LINE_WIDTH characters."""
        parts = []
        elapsed = self.elapsed()
        if self.unit is None or self.total is None:
            if self.current is not None and self.unit is not None:
                parts.append(_format_amount(self.current, self.unit))
            parts.append("elapsed {}".format(format_duration(elapsed)))
        else:
            if self.unit == "items":
                parts.append("{}/{}".format(_format_amount(self.current, self.unit),
                                            _format_amount(self.total, self.unit)))
            else:
                parts.append("{} / {}".format(_format_amount(self.current, self.unit),
                                              _format_amount(self.total, self.unit)))
            percent = self.percent()
            parts.append("--.- %" if percent is None else "{:.1f} %".format(percent))
        if self.unit == "B" and self.current is not None and elapsed > 0:
            parts.append(format_rate(self.current / elapsed))
        fps = self.fields.get("fps")
        if fps is not None:
            parts.append("{:.0f} fps".format(fps))
        speed = self.fields.get("speed")
        if speed is not None:
            parts.append("{:.1f}x".format(speed))
        eta = self.fields.get("eta")
        if eta is not None:
            parts.append("eta {}".format(format_duration(eta)))
        extra = self.fields.get("extra")
        if extra:
            parts.append(str(extra))
        return _fit_line(self.label, "  ".join(parts))

    def _render(self, now):
        self._last_render_at = now
        if self.quiet:
            return
        text = self.render_text()
        self._last_rendered_text = text
        if self.tty:
            # Pad with spaces so a shorter line fully covers the previous one.
            padding = " " * max(0, self._drawn_width - len(text))
            self._drawn_width = len(text)
            _write(self.stream, "\r" + text + padding)
        else:
            _write(self.stream, text + "\n")

    def finish(self, summary=None):
        """End the progress line and write the summary line, if any. Safe to call twice."""
        if self.finished:
            return
        if self._have_update and not self.quiet:
            final_text = self.render_text()
            if self.tty:
                if final_text != self._last_rendered_text or self._drawn_width == 0:
                    self._render(self.clock())
                _write(self.stream, "\n")
            elif final_text != self._last_rendered_text:
                self._render(self.clock())
        self.finished = True
        if summary is not None:
            say(summary, self.stream)


def ffmpeg_progress_callback(progress, total_seconds):
    """Adapter from vidfunnel.ffmpeg.run progress blocks to progress.update.

    A block carries 'out_time_s' (float seconds), 'fps' (float), 'speed'
    (float) and 'frame' (int); any may be missing or None early in a run.
    eta = (total_seconds - out_time_s) / speed, when total and a positive
    speed are known; never negative.
    """

    def on_block(block):
        out_time = block.get("out_time_s")
        speed = block.get("speed")
        eta = None
        if (total_seconds is not None and out_time is not None
                and speed is not None and speed > 0):
            eta = max(0.0, (total_seconds - out_time) / speed)
        progress.update(out_time, fps=block.get("fps"), speed=speed, eta=eta)

    return on_block


class StageSummary:
    """Per-outcome counts and elapsed time for the one-line end-of-stage summary.

    StageSummary(stage, outcomes=(), clock=time.monotonic). Outcomes named in
    `outcomes` are always listed, in that order, even at zero ('0 failed');
    others are listed after them in the order first added.
    """

    def __init__(self, stage, outcomes=(), clock=time.monotonic):
        self.stage = stage
        self.clock = clock
        self.started = clock()
        self.counts = {}
        for outcome in outcomes:
            self.counts[outcome] = 0

    def add(self, outcome, count=1):
        """Count one (or `count`) more of an outcome."""
        self.counts[outcome] = self.counts.get(outcome, 0) + count

    def elapsed(self):
        """Seconds since the summary was created, on its clock."""
        return max(0.0, self.clock() - self.started)

    def format(self, records_location=None):
        """'plan: 12 remux, 30 re-encode, 2 quarantine, 0 failed in 00:04:12; records in <location>'."""
        if self.counts:
            counted = ", ".join("{} {}".format(number, outcome)
                                for outcome, number in self.counts.items())
        else:
            counted = "no files"
        text = "{}: {} in {}".format(self.stage, counted, format_duration(self.elapsed()))
        if records_location:
            text += "; records in {}".format(records_location)
        return text

# End of File #
