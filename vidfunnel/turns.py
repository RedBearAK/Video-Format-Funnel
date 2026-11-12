"""A run's turns in folders: each folder's lock is taken when the run starts work there and released when it is done.

HANDOFF.md sections 6.2, 6.3 and 6.7. Several machines share a library
folder by folder: a run holds '.vidfunnel/lock' (vidfunnel.record.FolderLock)
only in the folders it is working in at that moment, so another host can
work in the others meanwhile. convert (and --refit) and plan --jobs build one
FolderTurns from the run's {folder: number of items (or, for plan, groups)}
and wrap each item's work in enter() and leave():

- enter(folder) takes the folder's lock the first time it is called for that
  folder and remembers the answer: HELD, SKIPPED (another run holds it:
  LockHeld) or ERROR (the lock file cannot be created: OSError; or no
  heartbeat thread can be started for it: LockNotStarted, a folder no other
  run holds and so never waited for; the folder named in `lock_errors`).
  The skip line ("skipping folder '<f>': ...") is printed once; the
  folder's later items get the remembered answer without another attempt.
  A per-folder threading.Lock lets only one thread acquire
  a given folder, so two workers that start items of one folder at once do
  not meet "locked by this run itself". Once the run is draining (SIGUSR1,
  a stop file, a lost lock) no new lock is taken: a folder not yet entered
  answers DRAINING, and the item is not started.
- leave(folder) counts the folder's items down; when the last one has
  ended it runs `last` (plan's folder file) under the lock and releases it.
- close() releases whatever is still held (a drain or an interrupt left
  items not run); callers call it from `finally`.

An item's work runs only when enter() returned HELD: vidfunnel.record's
lock check (ensure_lock) does nothing in a folder this process holds no lock
of, so nothing else would stop a write in a folder that was skipped or not
yet entered.

Later rounds (20261111). A folder that answered SKIPPED is not given up:
after the run's main pass, held_rounds() visits such folders again, round
after round, until none of them has work left (the other run finished it,
or this run took the lock and did it) or the run drains. Each round asks the
caller to settle, without a lock and writing nothing, what the other run has
already done (`settle`); for what is left, again(folder, count) forgets the
SKIPPED answer, so the next enter() tries the lock once more through the
normal FolderLock.acquire (which takes over a lock whose heartbeat is
stale), and the caller runs those items as in its main pass (`run`), each
through enter() and leave(). A round that could do nothing waits
HELD_RETRY_SECONDS (wait_for_folders), which a drain (SIGUSR1, a stop file)
or Ctrl+C ends. Folders that answered ERROR are never tried again. convert,
convert --refit and plan use it unless --no-wait is given.

A run never waits without saying for what: when a wait starts, and whenever
the set of folders it waits for or the holder of one of them changes, it
names each folder with its holder (host and pid from the last LockHeld), the
age of the holder's last beat, how long until a silent holder's lock is
taken over (or, where the heartbeat does not decide, what does), and how
many of this run's items are left there, and says what ends the wait; while
the set stays the same it repeats a short status line every HELD_REPORT_SECONDS; a
folder that leaves the set is named with the reason (taken by this run, its
lock free or taken over, or finished by its holder), and a wait cut short by
a drain, a stop file or an interrupt says so with the folders still held.
"""

VERSION = "20261111.1"

import threading
import time

import vidfunnel.record
from vidfunnel.interrupt import drain_before_next, drain_tally, draining
from vidfunnel.record import FolderLock, LockHeld, LockNotStarted

HELD = "held"
SKIPPED = "skipped"
ERROR = "error"
DRAINING = "draining"
RELEASED = "released"

# What a caller's item or group returns when enter() answered SKIPPED: kept for held_rounds, not counted yet.
HELD_ELSEWHERE = "held elsewhere"

HELD_RETRY_SECONDS = 60     # a round that could do nothing waits this long before the next one
HELD_POLL_SECONDS = 1.0     # while it waits it looks for a drain and for stop files this often
HELD_REPORT_SECONDS = 600   # while the folders waited for stay the same, a status line this often

_clock = time.monotonic     # the clock of the waits and their reports (tests replace it)

# Why wait_for_folders ended a wait early.
WAIT_DRAINING = "the run is draining (SIGUSR1)"
WAIT_DRAIN_FILE = "a stop file asks the run to drain"
WAIT_STOP_NOW = "a stop file asks the run to stop now"


class FolderTurns(object):
    """The folder locks of one run, each taken at the folder's first item and released after its last."""

    def __init__(self, counts, say_line, warn=None, on_enter=None, lock_errors=None):
        """counts: {folder: items}; say_line(message) prints a line; warn: the FolderLock's (default say_line).

        on_enter(folder), when given, runs under the new lock right after it
        is taken, before any item of the folder starts. lock_errors is the
        list the folders whose lock file could not be created are appended
        to (a new list when None).
        """
        self._counts = {str(folder): int(count) for folder, count in counts.items()}
        self._say = say_line
        self._warn = warn if warn is not None else say_line
        self._on_enter = on_enter
        self._guard = threading.Lock()
        self._folder_guards = {}
        self._states = {}
        self._locks = {}
        self._skip_said = set()  # folders whose "skipping folder" line was printed: once per folder for the run
        self._holders = {}       # folder -> the LockHeld of its last attempt, for held_rounds' lines
        self._again = set()      # folders again() opened, whose taking is announced
        self.lock_errors = lock_errors if lock_errors is not None else []

    def _folder_guard(self, name):
        with self._guard:
            return self._folder_guards.setdefault(name, threading.Lock())

    def state(self, folder):
        """The remembered answer for `folder`, or None when it has not been entered."""
        with self._guard:
            return self._states.get(str(folder))

    def enter(self, folder):
        """HELD, SKIPPED, ERROR or DRAINING (see the module docstring); the item runs only on HELD."""
        name = str(folder)
        known = self.state(name)
        if known is not None:
            return known
        with self._folder_guard(name):
            known = self.state(name)
            if known is not None:  # another thread entered it while this one waited
                return known
            if draining.is_set():
                return DRAINING
            lock = FolderLock(folder, warn=self._warn)
            try:
                lock.acquire()
            except LockNotStarted as error:  # no other run holds it: nothing to wait for
                self._say("skipping folder '{}': cannot take its lock: {}".format(folder, error))
                self.lock_errors.append(name)
                answer = ERROR
            except LockHeld as error:
                with self._guard:
                    self._holders[name] = error
                if name not in self._skip_said:
                    self._skip_said.add(name)
                    self._say("skipping folder '{}': {}".format(folder, error))
                answer = SKIPPED
            except OSError as error:
                self._say("skipping folder '{}': cannot take its lock: {}".format(folder, error))
                self.lock_errors.append(name)
                answer = ERROR
            else:
                with self._guard:
                    self._locks[name] = lock
                    again = name in self._again
                    self._again.discard(name)
                answer = HELD
                if again:
                    if getattr(lock, "took_over", False):
                        self._say("folder '{}' taken by this run in a later round: the lock of the run that held it "
                                  "was judged dead and taken over (see the warning above)".format(folder))
                    else:
                        self._say("folder '{}' taken by this run in a later round: the run that held it has "
                                  "released it".format(folder))
                if self._on_enter is not None:
                    try:
                        self._on_enter(folder)
                    except BaseException:
                        # Nothing of this folder runs without the check: released, its items not started.
                        self._release(name)
                        with self._guard:
                            self._states[name] = ERROR
                        raise
            with self._guard:
                self._states[name] = answer
            return answer

    def leave(self, folder, last=None):
        """One item of `folder` has ended; after its last one, last(folder) under the lock, then the release."""
        name = str(folder)
        with self._guard:
            left = self._counts.get(name, 0) - 1
            self._counts[name] = max(0, left)
            if left != 0 or name not in self._locks:
                return
        with self._folder_guard(name):
            try:
                if last is not None:
                    last(folder)
            finally:
                self._release(name)

    def _release(self, name):
        with self._guard:
            lock = self._locks.pop(name, None)
            if lock is not None:
                self._states[name] = RELEASED
        if lock is not None:
            lock.release()

    def again(self, folder, count):
        """Try `folder` once more: its next enter() takes the lock again, and `count` items will leave.

        For held_rounds, between rounds, when no item of the folder is in
        progress. Allowed only for a folder that answered SKIPPED (or that was
        never entered here: plan without --jobs takes its first locks without
        a FolderTurns) and whose items have all left; anything else raises
        ValueError, since a folder this run holds, or one whose lock file could
        not be created, must not be entered again. The skip line is not
        printed again for it.
        """
        name = str(folder)
        with self._folder_guard(name):
            with self._guard:
                state = self._states.get(name)
                if state not in (None, SKIPPED) or self._counts.get(name, 0) != 0:
                    raise ValueError("folder '{}' cannot be tried again (state {}, {} item(s) not ended)".format(
                        folder, state, self._counts.get(name, 0)))
                self._states.pop(name, None)
                self._counts[name] = int(count)
                self._skip_said.add(name)
                self._again.add(name)

    def holder(self, folder):
        """The LockHeld of the last attempt on `folder` that found it held, or None."""
        with self._guard:
            return self._holders.get(str(folder))

    def held(self):
        """The folders whose lock this run holds now, in no particular order."""
        with self._guard:
            return list(self._locks)

    def close(self):
        """Release every lock still held."""
        for name in self.held():
            self._release(name)


def _seconds(value):
    return "{:.0f} s".format(max(0.0, value))


def holder_words(error):
    """'held by host 'H' pid P, last beat N s ago (...)' from a LockHeld, for the lines of a wait.

    Each form says what ends the wait for that folder: the heartbeat's
    silence, its pid's end on this machine (pid_running_here) or, embedded,
    the LockHeld message, which says which rule keeps the lock.
    """
    if error is None:
        return "held by another run"
    words = "held by another run" if error.host is None else "held by host '{}' pid {}".format(error.host, error.pid)
    if getattr(error, "pid_running_here", False):
        return ("{}, a process running on this machine (taken over once that process is gone; while it runs, not "
                "before the lock is {:g} h old: {})".format(words, vidfunnel.record.LOCK_STALE_SECONDS / 3600.0,
                                                           error))
    beat = getattr(error, "beat_age_seconds", None)
    if beat is None:
        return "{} (its heartbeat does not decide here: {})".format(words, error)
    stale = vidfunnel.record.HEARTBEAT_STALE_SECONDS
    return "{}, last beat {} ago (taken over if it stays silent for {} more, at the first try after that)".format(
        words, _seconds(beat), _seconds(stale - beat))


def _holder_key(error):
    """(host, pid) of a LockHeld, or None: a change re-announces the wait."""
    return None if error is None else (error.host, error.pid)


def _beat_words(error):
    beat = getattr(error, "beat_age_seconds", None) if error is not None else None
    return "last beat not judged by heartbeat" if beat is None else "last beat {} ago".format(_seconds(beat))


def waiting_lines(turns, folders, left):
    """The lines printed when a wait starts or its folders change: each folder, its holder, what ends the wait.

    `left`: {folder: items of this run still to do there}.
    """
    lines = ["waiting for {} folder(s) other runs hold, trying each again every {:g} s:".format(
        len(folders), HELD_RETRY_SECONDS)]
    for folder in folders:
        lines.append("  '{}': {}; {} item(s) of this run left there".format(folder, holder_words(turns.holder(folder)),
                                                                            left[folder]))
    lines.append("the wait ends for a folder when its holder finishes or releases it, or when its lock is taken over "
                 "as its line says; Ctrl+C, SIGUSR1 or 'vidfunnel stop-all PATH' (PATH at or above these folders) "
                 "ends the whole wait; --no-wait skips such folders instead")
    return lines


def status_line(turns, folders, waited):
    """The short line repeated every HELD_REPORT_SECONDS while the folders waited for stay the same."""
    return "still waiting after {} for {} folder(s) other runs hold: {}".format(
        _seconds(waited), len(folders), ", ".join("'{}' ({})".format(folder, _beat_words(turns.holder(folder)))
                                                  for folder in folders))


def wait_for_folders(folders):
    """Wait HELD_RETRY_SECONDS before the next round; None when it passed, else why the wait ends early.

    Looks every HELD_POLL_SECONDS for the stop files above each folder
    (vidfunnel.stopfile.check_item: a 'drain' file sets the drain and gives
    WAIT_DRAIN_FILE, a 'now' file WAIT_STOP_NOW) and for a drain (SIGUSR1:
    WAIT_DRAINING). Ctrl+C interrupts the sleep on this (the main) thread.
    """
    from vidfunnel.stopfile import check_item
    deadline = _clock() + HELD_RETRY_SECONDS
    while True:
        for folder in folders:
            found = check_item(folder)
            if found == "now":
                return WAIT_STOP_NOW
            if found == "drain":
                return WAIT_DRAIN_FILE
        if draining.is_set():
            return WAIT_DRAINING
        remaining = deadline - _clock()
        if remaining <= 0:
            return None
        time.sleep(min(remaining, HELD_POLL_SECONDS))


def held_rounds(turns, held, folder_of, settle, run, say_line, size_of=None):
    """Visit the folders other runs held again until none has work left; return the units still held at a drain.

    `held`: the units (convert's items, plan's groups) whose folder answered
    SKIPPED, in list order; folder_of(unit) names its folder and size_of(unit)
    the items in it (1 when None). Each round: settle(unit) returns what is
    left of the unit to do under the folder's lock (the unit itself, a
    smaller unit, or None when the other run has done it all; it prints and
    counts what it settled, takes no lock and writes nothing); for each
    folder with something left, turns.again() resets it, and run(units) does
    those units as the caller's main pass does, each through turns.enter()
    and turns.leave(), and returns the ones whose folder another run still
    holds. A round in which nothing was settled and no unit ran is followed
    by a wait (wait_for_folders), announced by waiting_lines (folders in
    sorted order) whenever the set of folders waited for, or the holder (host
    and pid) of one of them, is not the one last announced, and by a
    status_line every HELD_REPORT_SECONDS while it is. Returns [] when no
    folder has work left, or the units still held when the run drains (the
    caller counts them as skipped, and they are added to drain_tally.left,
    as never started); Ctrl+C and a 'now' stop file raise KeyboardInterrupt,
    after a line that says so.
    """
    size_of = size_of if size_of is not None else (lambda unit: 1)

    def left_in(units):
        left = {}
        for unit in units:
            name = str(folder_of(unit))
            left[name] = left.get(name, 0) + size_of(unit)
        return left

    def give_up(units, reason, counted):
        left = left_in(units)
        if counted:  # a drain: these items were never started, as the drain's 'left' counts them
            drain_tally.seen = True
            drain_tally.left += sum(left.values())
        say_line("not waiting for {} folder(s) other runs hold ({}): {}; {}".format(
            len(left), ", ".join("'{}'".format(name) for name in left), reason,
            "their {} item(s) are counted as skipped".format(sum(left.values())) if counted
            else "nothing more is done there"))

    announced = None
    reported = None
    since = None
    while held:
        if drain_before_next(0):
            give_up(held, "the run is draining (SIGUSR1, a stop file or a lost lock; said above)", True)
            return held
        by_folder = {}
        for unit in held:
            by_folder.setdefault(str(folder_of(unit)), []).append(unit)
        moved = False
        needed = []
        for name, units in by_folder.items():
            left = []
            for unit in units:
                rest = settle(unit)
                if rest is not unit:
                    moved = True
                if rest is not None:
                    left.append(rest)
            if left:
                turns.again(folder_of(left[0]), len(left))
                needed.extend(left)
            else:
                say_line("folder '{}' needs nothing more from this run: the run that held it has dealt with its {} "
                         "item(s)".format(name, sum(size_of(unit) for unit in units)))
        if not needed:
            return []
        held = run(needed)
        if len(held) != len(needed):
            moved = True
        if not held or moved:
            continue
        left = left_in(held)
        folders = sorted(left)  # with --jobs `held` comes in completion order: the set and its holders decide
        holders = {name: _holder_key(turns.holder(name)) for name in folders}
        now = _clock()
        if holders != announced:
            for line in waiting_lines(turns, folders, left):
                say_line(line)
            announced = holders
            reported = now
            since = now if since is None else since
        elif now - reported >= HELD_REPORT_SECONDS:
            say_line(status_line(turns, folders, now - since))
            reported = now
        try:
            reason = wait_for_folders(folders)
        except KeyboardInterrupt:
            give_up(held, "interrupted (Ctrl+C or a stop signal)", False)
            raise
        if reason == WAIT_STOP_NOW:
            give_up(held, reason, False)
            raise KeyboardInterrupt()
        if reason is not None:
            drain_tally.seen = True
            give_up(held, reason, True)
            return held
    return held


def not_started(weight=1):
    """For run_pool's on_done: a task that returned without starting (DRAINING) was counted finished; count it left."""
    from vidfunnel.interrupt import drain_tally
    drain_tally.finished -= weight
    drain_tally.left += weight
    drain_tally.seen = True


# End of File #
