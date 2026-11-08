"""Ctrl+C with --jobs: one event that tells the worker threads the user has interrupted the run.

Ctrl+C sends SIGINT to the whole process group, so every running ffmpeg,
ffprobe and par2 child dies, but only the main thread receives
KeyboardInterrupt. A worker would see its dead child as an ordinary failed
run and write that into a record. The contract instead:

- `interrupted` is a threading.Event. run_pool() clears it before the pool
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
vidfunnel.cli installs a handler that sets `interrupted` itself, sends
SIGTERM to the children registered in vidfunnel.ffmpeg (which then end
the way child_interrupted() expects), and raises a KeyboardInterrupt on
the main thread; the contract above then applies unchanged.
"""

VERSION = "20261107"

import queue
import threading

# Set only by run_pool() on the main thread while a pool is running; see the module docstring.
interrupted = threading.Event()

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


def run_pool(jobs, tasks, on_done):
    """Run each task (a callable without arguments) on `jobs` threads; on_done(index, value) on this thread.

    A fixed set of worker threads takes the tasks from one queue; on_done is
    called on the calling thread for each task as it finishes, in the order
    they finish. Every worker is put in the list of threads before it is
    started, and all of them are joined before this function returns or
    raises, so no task outlives it. A worker takes no further task once
    `interrupted` (or, after any other exception here, the local `stop`) is
    set. On KeyboardInterrupt on this thread (or a task that raised
    Interrupted): `interrupted` is set, the running tasks are waited for,
    and KeyboardInterrupt is raised; a task that ended with Interrupted is
    never passed to on_done. Another exception from a task is raised here
    after the running tasks have ended.
    """
    pending = queue.Queue()
    for index, task in enumerate(tasks):
        pending.put((index, task))
    finished = queue.Queue()
    stop = threading.Event()

    def worker():
        while not (stop.is_set() or interrupted.is_set()):
            try:
                index, task = pending.get_nowait()
            except queue.Empty:
                return
            try:
                finished.put((index, True, task()))
            except BaseException as error:  # handed to the calling thread, which raises it
                finished.put((index, False, error))

    interrupted.clear()
    threads = []
    try:
        try:
            for _number in range(min(jobs, pending.qsize())):
                thread = threading.Thread(target=worker, name="vidfunnel-worker")
                threads.append(thread)
                thread.start()
            for _number in range(len(tasks)):
                index, ok, value = finished.get()
                if not ok:
                    raise value
                on_done(index, value)
        except (KeyboardInterrupt, Interrupted) as error:
            interrupted.set()
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
