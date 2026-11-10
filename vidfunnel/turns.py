"""A run's turns in folders: each folder's lock is taken when the run starts work there and released when it is done.

HANDOFF.md sections 6.2, 6.3 and 6.7. Several machines share a library
folder by folder: a run holds '.vidfunnel/lock' (vidfunnel.record.FolderLock)
only in the folders it is working in at that moment, so another host can
work in the others meanwhile. convert (and --refit) and plan --jobs build one
FolderTurns from the run's {folder: number of items (or, for plan, groups)}
and wrap each item's work in enter() and leave():

- enter(folder) takes the folder's lock the first time it is called for that
  folder and remembers the answer: HELD, SKIPPED (another run holds it:
  LockHeld) or ERROR (the lock file cannot be created: OSError, the folder
  named in `lock_errors`). The skip line ("skipping folder '<f>': ...") is
  printed once; the folder's later items get the remembered answer without
  another attempt. A per-folder threading.Lock lets only one thread acquire
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
"""

VERSION = "20261110"

import threading

from vidfunnel.interrupt import draining
from vidfunnel.record import FolderLock, LockHeld

HELD = "held"
SKIPPED = "skipped"
ERROR = "error"
DRAINING = "draining"
RELEASED = "released"


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
            except LockHeld as error:
                self._say("skipping folder '{}': {}".format(folder, error))
                answer = SKIPPED
            except OSError as error:
                self._say("skipping folder '{}': cannot take its lock: {}".format(folder, error))
                self.lock_errors.append(name)
                answer = ERROR
            else:
                with self._guard:
                    self._locks[name] = lock
                answer = HELD
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

    def held(self):
        """The folders whose lock this run holds now, in no particular order."""
        with self._guard:
            return list(self._locks)

    def close(self):
        """Release every lock still held."""
        for name in self.held():
            self._release(name)


def not_started(weight=1):
    """For run_pool's on_done: a task that returned without starting (DRAINING) was counted finished; count it left."""
    from vidfunnel.interrupt import drain_tally
    drain_tally.finished -= weight
    drain_tally.left += weight
    drain_tally.seen = True


# End of File #
