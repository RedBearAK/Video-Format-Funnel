"""Two convert processes on one library: each works in the folders the other does not hold at that moment.

Each child runs vidfunnel.encode.convert_paths through a small driver that
replaces the encode with a stand-in: it writes a marker file naming the
folder, the record and its pid, waits for a gate file the test controls,
checks its folder lock (vidfunnel.record.ensure_lock), writes a stand-in
output and saves a convert stage. No ffmpeg runs.
"""

VERSION = "20261110.1"

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import vidfunnel.record
from vidfunnel.record import load_record, machine_identity, new_record, save_record, set_status

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEADLINE_SECONDS = 180.0
FOLDERS = ("harbour", "meadow", "orchard", "valley")
STEMS = ("dawn", "dusk")

DRIVER = r'''
import contextlib
import os
import sys
import time
import types
from pathlib import Path
from unittest import mock

library, markers, gate, pose = sys.argv[1:5]

import vidfunnel.encode as encode
from vidfunnel.record import add_stage, cheap_fingerprint, ensure_lock, save_record, set_status


def stand_in(tools, key, record, options, progress=None, position=None, total=None):
    key = Path(key)
    if record.get("status") != "planned":
        return {"outcome": "skipped", "skipped": "status is '{}'".format(record.get("status"))}
    marker = Path(markers) / "{}--{}--{}".format(key.parent.name, key.stem, os.getpid())
    marker.write_text("started\n")
    deadline = time.monotonic() + 600
    while not os.path.exists(gate):
        if time.monotonic() > deadline:
            raise SystemExit("the gate '{}' never opened".format(gate))
        time.sleep(0.02)
    ensure_lock(key.parent)
    output = key.parent / record["output"]["name"]
    output.write_bytes(("stand-in output of " + key.stem + " ").encode() * 16)
    record["output"]["fingerprint"] = cheap_fingerprint(output)
    add_stage(record, "convert", {"outcome": "ok", "pid": os.getpid()})
    set_status(record, "converted")
    save_record(key, record)
    return {"outcome": "ok"}


with contextlib.ExitStack() as stack:
    if pose != "-":
        stack.enter_context(mock.patch("vidfunnel.record.machine_identity",
                                       return_value=(pose + "-machine", "machine-id")))
        stack.enter_context(mock.patch("vidfunnel.record.boot_id", return_value=pose + "-boot"))
        stack.enter_context(mock.patch("socket.gethostname", return_value=pose + "-host"))
    stack.enter_context(mock.patch.object(encode, "convert_one", stand_in))
    options = types.SimpleNamespace(par2=None, jobs=1, dry_run=False, verbose=False, refit=False)
    progress = types.SimpleNamespace(stream=None, quiet=True, verbose=False)
    summary = encode.convert_paths(None, [library], options, progress)
failed = summary.counts.get("failed", 0) + summary.counts.get("collision", 0)
sys.exit(1 if failed or summary.lock_errors else 0)
'''


def wait_for(condition, deadline=DEADLINE_SECONDS):
    """Poll `condition` until it is true; False only when the generous deadline passes first."""
    ends = time.monotonic() + deadline
    while time.monotonic() < ends:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


class TwoRunsOneLibraryTest(unittest.TestCase):

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.library = base / "library"
        self.markers = base / "markers"
        self.markers.mkdir()
        self.driver = base / "driver.py"
        self.driver.write_text(DRIVER, encoding="utf-8")
        self.gates = {"first": base / "gate-first", "second": base / "gate-second"}
        self.logs = base / "logs"
        self.logs.mkdir()
        for folder in FOLDERS:
            (self.library / folder).mkdir(parents=True)
            for stem in STEMS:
                source = self.library / folder / (stem + ".avi")
                source.write_bytes(("source of {} in {} ".format(stem, folder)).encode() * 64)
                record = new_record(source)
                record["action"] = "remux"
                record["output"]["name"] = stem + ".mp4"
                set_status(record, "planned")
                save_record(source, record)

    def start(self, name, pose):
        log = open(str(self.logs / (name + ".log")), "w")
        self.addCleanup(log.close)
        child = subprocess.Popen([sys.executable, str(self.driver), str(self.library), str(self.markers),
                                  str(self.gates[name]), pose],
                                 cwd=REPO_ROOT, env=dict(os.environ, PYTHONPATH=REPO_ROOT),
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        self.addCleanup(self.stop, child)
        return child

    def stop(self, child):
        if child.poll() is None:
            child.kill()
            child.wait()

    def output(self, name):
        return (self.logs / (name + ".log")).read_text(encoding="utf-8")

    def markers_of(self, pid):
        return sorted(marker.name.rsplit("--", 1)[0] for marker in self.markers.iterdir()
                      if marker.name.endswith("--{}".format(pid)))

    def lock_of(self, folder):
        return self.library / folder / ".vidfunnel" / "lock"

    def two_runs(self, poses, judged_by):
        """`judged_by`: words of the reason the second run gives for skipping the first run's folder; a
        '{first_pid}' in it stands for the first run's pid."""
        first = self.start("first", poses[0])
        self.assertTrue(wait_for(lambda: self.markers_of(first.pid) == ["harbour--dawn"]
                                 and self.lock_of("harbour").exists()),
                        "the first run never reached its gate in 'harbour': " + self.output("first"))
        second = self.start("second", poses[1])
        self.assertTrue(wait_for(lambda: self.markers_of(second.pid) == ["meadow--dawn"] or second.poll() is not None),
                        "the second run never started work in 'meadow': " + self.output("second"))
        self.assertEqual(self.markers_of(second.pid), ["meadow--dawn"], self.output("second"))
        # The first run is still held at its gate in 'harbour' while the second works in 'meadow'.
        self.assertIsNone(first.poll(), self.output("first"))
        self.assertEqual(self.markers_of(first.pid), ["harbour--dawn"])
        self.assertTrue(self.lock_of("harbour").exists())

        self.gates["first"].write_text("open\n")
        first.wait(DEADLINE_SECONDS)
        self.gates["second"].write_text("open\n")
        second.wait(DEADLINE_SECONDS)
        first_text, second_text = self.output("first"), self.output("second")
        self.assertEqual(first.returncode, 0, first_text)
        self.assertEqual(second.returncode, 0, second_text)

        all_markers = [marker.name.rsplit("--", 1)[0] for marker in self.markers.iterdir()]
        expected = sorted("{}--{}".format(folder, stem) for folder in FOLDERS for stem in STEMS)
        self.assertEqual(sorted(all_markers), expected, "a record was started twice, or never")
        for folder in FOLDERS:
            for stem in STEMS:
                record = load_record(self.library / folder / (stem + ".avi"))
                self.assertEqual(record["status"], "converted", (folder, stem))
                self.assertIn("convert", record["stages"], (folder, stem))
        self.assertEqual(self.markers_of(second.pid), ["meadow--dawn", "meadow--dusk"], second_text)
        self.assertEqual(self.markers_of(first.pid), [name for name in expected if not name.startswith("meadow")])
        self.assertIn("skipping folder '{}': ".format(self.library / "harbour"), second_text)
        self.assertIn("skipping folder '{}': ".format(self.library / "meadow"), first_text)
        skip_line = [line for line in second_text.splitlines() if line.startswith("skipping folder")][0]
        self.assertIn(judged_by.format(first_pid=first.pid), skip_line)
        for text in (first_text, second_text):
            self.assertNotIn("taking over", text)
            self.assertNotIn("Traceback", text)
        for stem in STEMS:  # in 'orchard' and 'valley', which the first run finished meanwhile
            self.assertEqual(second_text.count("skipping '{}.avi': already converted".format(stem)), 2, second_text)
        for folder in FOLDERS:
            self.assertFalse(self.lock_of(folder).exists(), folder)

    def test_two_runs_on_this_machine_share_the_library_folder_by_folder(self):
        """The flock rule judges the first run's lock only where the lock records a kernel boot id (Linux).
        macOS has none, so there the lock is judged by its pid, running on this machine, and its heartbeat (CI
        run 42840627946); with no machine identity either (a random uuid.getnode), by its heartbeat alone."""
        if vidfunnel.record.fcntl is not None and vidfunnel.record.boot_id() is not None:
            judged_by = "still holds the lock file's flock on this machine"
        elif machine_identity()[0] is not None:
            judged_by = "its pid {first_pid} is running here and its holder last beat"
        else:
            judged_by = "its holder last beat"
        self.two_runs(("-", "-"), judged_by)

    def test_two_runs_posing_as_two_machines_share_the_library_folder_by_folder(self):
        self.two_runs(("lighthouse", "windmill"), "its holder last beat")


if __name__ == "__main__":
    unittest.main()

# End of File #
