"""Ctrl+C with --jobs: one event that tells the worker threads the user has interrupted the run.

Ctrl+C sends SIGINT to the whole process group, so every running ffmpeg,
ffprobe and par2 child dies, but only the main thread receives
KeyboardInterrupt. A worker would see its dead child as an ordinary failed
run and write that into a record. The contract instead:

- `interrupted` is a Flag, a threading.Event without its lock (see
  Flag for why). run_pool() clears it before the pool
  starts and again once no worker is alive; it sets it on the main thread,
  as the first thing it does when KeyboardInterrupt arrives there.
- The subprocess entry points (vidfunnel.ffmpeg.run, encode.write_par2) and
  the hashing loops read it: with the event set they raise Interrupted
  instead of starting a child or reporting the exit of one the Ctrl+C
  killed (child_interrupted()). Interrupted is not a subclass of FfmpegError
  or OSError, so the handlers that save a 'failed' record never catch it,
  and the worker's item ends without saving its record again (a state saved
  before the Ctrl+C stays, as after Ctrl+C without --jobs).
- run_pool() starts no further task, waits for the running ones (which
  end at their next check), and raises KeyboardInterrupt again;
  vidfunnel.cli.main prints 'vidfunnel: interrupted'.

SIGTERM goes to the Python process alone, so its children do not see it.
vidfunnel.cli installs a handler that raises Terminated, a
KeyboardInterrupt, on the main thread at the first SIGTERM and takes no
lock. The contract above then applies, with one
addition: run_pool() calls stop_workers(), which sets `interrupted` and
sends SIGTERM to the children registered in vidfunnel.ffmpeg (which then
end the way child_interrupted() expects); vidfunnel.cli.main calls it
again after a SIGTERM, for the case without a pool. SIGHUP and SIGQUIT are
handled the same way.

SIGUSR1 drains: vidfunnel.cli's handler sets `draining` (a Flag, so the
handler takes no lock). run_pool() then hands out no further task, and the
sequential loops of plan, convert (and --refit), verify and retire ask
drain_before_next() before each next item; the tasks in progress run to their end, nothing signals their
children and `interrupted` stays clear, so their results are applied as
usual. The main thread counts, in `drain_tally`, the tasks that were still
running when the drain began and the items never started, for the line
vidfunnel.cli.main prints at the end.
"""

VERSION = "20261109"

import queue
import threading
import time


class Flag:
    """threading.Event's set/clear/is_set/wait without its lock.

    A KeyboardInterrupt or Terminated can be raised on the main thread at
    almost any bytecode, including inside Event.set() while the Event's
    Condition lock is held; that lock then stays held and the next set()
    waits forever (34 of 300 test runs that sent SIGTERM to a loop of
    Event.clear() and Event.set() hung that way). Here set(), clear() and
    is_set() are a single attribute store or read, which no exception can
    leave half done, and wait() polls the attribute every POLL_SECONDS.
    """

    POLL_SECONDS = 0.01

    def __init__(self):
        self._value = False

    def set(self):
        self._value = True

    def clear(self):
        self._value = False

    def is_set(self):
        return self._value

    def wait(self, timeout=None):
        """True as soon as the flag is set, False when `timeout` seconds pass first (None: no limit)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._value:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(self.POLL_SECONDS)
        return True


# Set only on the main thread: by run_pool() while a pool runs, and by stop_workers(); see the module docstring.
interrupted = Flag()

# Set by the SIGUSR1 handler (vidfunnel.cli): start no further item; see the module docstring.
draining = Flag()


class DrainTally:
    """What a drain did, counted on the main thread: `finished` items were in progress when it began
    and ran to their end, `left` items were never started; `seen` once a loop has stopped for it."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.seen = False
        self.finished = 0
        self.left = 0


drain_tally = DrainTally()


def drain_before_next(left, folder=None):
    """For sequential loops, before each next item: True (stop here) while draining, counting `left` items.

    With `folder`, the stop files above it are checked first
    (vidfunnel.stopfile.check_item): 'drain' sets the drain, 'now' raises
    KeyboardInterrupt here, as Ctrl+C would on this (the main) thread.
    """
    if folder is not None:
        from vidfunnel.stopfile import check_item
        if check_item(folder) == "now":
            raise KeyboardInterrupt()
    if not draining.is_set():
        return False
    drain_tally.seen = True
    drain_tally.left += left
    return True


def drain_after_item(count=1):
    """For sequential loops, after each item: count it as finished in the drain when the drain has begun.

    It marks the drain as seen, so a drain that began during the last item
    is reported with its counts, not as a run that ignored it.
    """
    if draining.is_set():
        drain_tally.seen = True
        drain_tally.finished += count

# A worker whose child died the way a Ctrl+C kills it waits this long for the main
# thread to set `interrupted`, so the child's death and the main thread's
# KeyboardInterrupt, which arrive in either order, are not taken for a failure.
GRACE_SECONDS = 2.0

# 255: ffmpeg's status after it caught SIGINT (tested with ffmpeg 7.1.5); 130: 128 + SIGINT, a
# shell wrapper's status for a child killed by SIGINT; a negative status: killed by a signal.
_SIGNAL_STATUSES = (255, 130)


class Interrupted(Exception):
    """The user pressed Ctrl+C while a pool ran; the item in progress stops without writing its record."""


def check():
    """Raise Interrupted when the event is set; a no-op otherwise."""
    if interrupted.is_set():
        raise Interrupted("interrupted by the user")


def child_interrupted(returncode):
    """True when a child that exited with `returncode` was stopped by the user's Ctrl+C.

    Status 0 is never an interruption. With the event set, any other status
    is (the item is then redone by the next run, so a real failure that
    coincides with Ctrl+C costs a repeat, not a false record). With the
    event clear, a signal-like status on a worker thread waits up to
    GRACE_SECONDS for the main thread to set it; on the main thread, which
    receives KeyboardInterrupt itself, nothing waits and nothing changes.
    """
    if returncode == 0:
        return False
    if interrupted.is_set():
        return True
    if returncode < 0 or returncode in _SIGNAL_STATUSES:
        if threading.current_thread() is not threading.main_thread():
            return interrupted.wait(GRACE_SECONDS)
    return False


def stop_workers():
    """Set `interrupted` and send SIGTERM to the registered children; never in a signal handler.

    Called on the main thread after Ctrl+C or a stop signal, and on any
    thread that finds a 'now' stop file (vidfunnel.stopfile.check_item).

    A KeyboardInterrupt (a further Ctrl+C, or a SIGTERM after a Ctrl+C) raised while it runs
    is dropped and the steps run again, so both are done when it returns;
    the caller is already handling the first interrupt. Sending SIGTERM to
    children a Ctrl+C has already stopped is harmless.
    """
    from vidfunnel.ffmpeg import terminate_children  # here: vidfunnel.ffmpeg imports this module
    while True:
        try:
            interrupted.set()
            terminate_children()
            return
        except KeyboardInterrupt:
            continue


def run_pool(jobs, tasks, on_done, sizes=None, folders=None):
    """Run each task (a callable without arguments) on `jobs` threads; on_done(index, value) on this thread.

    A fixed set of worker threads takes the tasks from one queue; on_done is
    called on the calling thread for each task as it finishes, in the order
    they finish. Every worker is put in the list of threads before it is
    started, and all of them are joined before this function returns or
    raises, so no task outlives it. A worker takes no further task once
    `interrupted` (or, after any other exception here, the local `stop`) is
    set. On KeyboardInterrupt on this thread (or a task that raised
    Interrupted): stop_workers() sets `interrupted` and sends SIGTERM to
    the registered children, the running tasks are waited for,
    and KeyboardInterrupt is raised; a task that ended with Interrupted is
    never passed to on_done. Another exception from a task is raised here
    after the running tasks have ended.

    Once `draining` is set a worker takes no further task, and this function
    returns normally when the running ones have ended: each worker puts a
    final None on the result queue as it exits, and results are collected
    until every worker has done so. drain_tally then counts the tasks that
    ended while draining as finished and the untaken ones as left, each
    weighed by sizes[index] (the items in that task; 1 when sizes is None).

    With `folders` (the folder of each task), a worker that has taken a task
    first checks the stop files above its folder (vidfunnel.stopfile.check_item):
    on 'drain' or 'now' it puts the task back untouched and takes no more;
    'now' has also set `interrupted` and signalled the children, so this
    function then raises KeyboardInterrupt as after Ctrl+C.
    """
    weights = list(sizes) if sizes is not None else [1] * len(tasks)
    pending = queue.Queue()
    for index, task in enumerate(tasks):
        pending.put((index, task))
    finished = queue.Queue()
    stop = threading.Event()

    def worker():
        try:
            while not (stop.is_set() or interrupted.is_set() or draining.is_set()):
                try:
                    index, task = pending.get_nowait()
                except queue.Empty:
                    return
                if folders is not None:
                    from vidfunnel.stopfile import check_item
                    if check_item(folders[index]) is not None:
                        pending.put((index, task))  # not started: counted as left, or dropped by the interrupt
                        return
                try:
                    value = task()
                    finished.put((index, True, value, draining.is_set()))
                except BaseException as error:  # handed to the calling thread, which raises it
                    finished.put((index, False, error, False))
        finally:
            finished.put(None)  # this worker takes no further task

    interrupted.clear()
    threads = []
    try:
        try:
            for _number in range(min(jobs, pending.qsize())):
                thread = threading.Thread(target=worker, name="vidfunnel-worker")
                threads.append(thread)
                thread.start()
            working = len(threads)
            while working:
                result = finished.get()
                if result is None:
                    working -= 1
                    continue
                index, ok, value, while_draining = result
                if not ok:
                    raise value
                if while_draining:
                    drain_tally.finished += weights[index]
                on_done(index, value)
            if interrupted.is_set():  # a 'now' stop file, found by a worker before any task raised
                raise KeyboardInterrupt()
            if draining.is_set():
                drain_tally.seen = True
                while True:
                    try:
                        index, _task = pending.get_nowait()
                    except queue.Empty:
                        break
                    drain_tally.left += weights[index]
        except (KeyboardInterrupt, Interrupted) as error:
            stop_workers()
            if isinstance(error, Interrupted):
                raise KeyboardInterrupt() from error
            raise
        finally:
            stop.set()
            for thread in threads:
                thread.join()
    finally:
        # A second Ctrl+C can cut the joins short; the event then stays set for the workers still alive.
        if not any(thread.is_alive() for thread in threads):
            interrupted.clear()


# End of File #
