"""Tests for the modes of what vidfunnel creates in a library folder (vidfunnel.record.take_folder_mode).

A file vidfunnel creates in a library folder gains the bits of the folder's
mode without the execute, setuid, setgid and sticky bits; a directory gains
the folder's mode as it is. Bits are only added, never removed, so every
expected mode below is the mode the creating call gave (its mode argument
less the umask in force) OR'ed with what the folder grants. Modes are read
with stat, never by trying to open a file that should be denied, so the
tests mean the same when they run as root.

os.umask is process-wide: a test that sets it restores it with addCleanup and
runs no thread of the package while it is set (no lock is held there, so no
heartbeat thread runs). The tests that hold a lock keep the process's umask:
the lock files are created with mode 0644, which differs from the expected
mode for the folders they use whatever the umask. The end-to-end runs set
their umask in a shell of their own.
"""

VERSION = "20261110"

import argparse
import contextlib
import datetime
import errno
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import vidfunnel.record as record_module
import vidfunnel.stopfile
from vidfunnel import encode
from vidfunnel import retire
from vidfunnel.ffmpeg import ToolNotFound, find_tools
from vidfunnel.plan import make_options, plan_paths
from vidfunnel.record import (
    FOLDER_FILE,
    LOCK_NAME,
    PROBE_PREFIX,
    RECORD_DIR,
    TAKEOVER_MARKER,
    FolderLock,
    RecordError,
    load_record,
    make_record_dir,
    record_dir,
    record_path,
    save_folder,
    save_record,
    take_folder_mode,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(REPO_ROOT, "tests", "fixtures")
SUBTITLE_SOURCE = "h264_mkv_srt_subtitles.mkv"
SUBTITLE_OUTPUT = "h264_mkv_srt_subtitles.mp4"
SUBTITLE_SIDECAR = "h264_mkv_srt_subtitles.srt"

# Folder mode -> the bits it grants a file created in it.
FILE_MODES = {0o700: 0o600, 0o750: 0o640, 0o755: 0o644, 0o775: 0o664, 0o2775: 0o664, 0o777: 0o666}
UMASKS = (0o022, 0o002, 0o077)


def mode_of(path):
    return stat.S_IMODE(os.stat(str(path)).st_mode)


def set_umask(test, value):
    """Set the process umask for this test; restored by a cleanup."""
    previous = os.umask(value)
    test.addCleanup(os.umask, previous)


def created_mode(folder, mode):
    """The mode a file created with `mode` gets in `folder` under the process umask (read without setting it)."""
    probe = Path(folder) / "umask-probe"
    os.close(os.open(str(probe), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode))
    try:
        return mode_of(probe)
    finally:
        probe.unlink()


def setgid_kept(folder):
    """True when a directory made in `folder` keeps a setgid bit set with chmod (some systems drop it)."""
    probe = Path(folder) / "setgid-probe"
    probe.mkdir()
    try:
        os.chmod(str(probe), 0o2775)
        return bool(os.stat(str(probe)).st_mode & stat.S_ISGID)
    finally:
        probe.rmdir()


def _tools_or_none():
    try:
        return find_tools()
    except ToolNotFound:
        return None


TOOLS = _tools_or_none()


def quiet():
    return types.SimpleNamespace(stream=io.StringIO(), quiet=True, verbose=False)


def convert_options(**values):
    defaults = dict(par2=None, verbose=False, jobs=1, dry_run=False, refit=False)
    defaults.update(values)
    return types.SimpleNamespace(**defaults)


class TempCase(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.base = Path(holder.name)

    def library(self, name, mode):
        """A folder under base with exactly `mode` (chmod, so the umask does not enter)."""
        folder = self.base / name
        folder.mkdir()
        os.chmod(str(folder), mode)
        return folder


# ---------------------------------------------------------------- the helper


class TakeFolderModeTest(TempCase):

    def test_files_and_directories_gain_the_folders_mode_under_every_umask(self):
        for folder_mode, file_mode in sorted(FILE_MODES.items()):
            for umask in UMASKS:
                with self.subTest(folder="{:o}".format(folder_mode), umask="{:03o}".format(umask)):
                    folder = self.library("library-{:o}-{:03o}".format(folder_mode, umask), folder_mode)
                    if mode_of(folder) != folder_mode:
                        self.skipTest("this system does not keep mode {:o} on a folder (it has {:o})".format(
                            folder_mode, mode_of(folder)))
                    previous = os.umask(umask)
                    try:
                        by_path = folder / "by-path.json"
                        os.close(os.open(str(by_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666))
                        take_folder_mode(by_path, folder)
                        handle = os.open(str(folder / "by-descriptor.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                         0o600)
                        try:
                            take_folder_mode(handle, folder)
                        finally:
                            os.close(handle)
                        made = make_record_dir(folder)
                        child = folder / "child"
                        child.mkdir()
                        take_folder_mode(child, folder, directory=True)
                    finally:
                        os.umask(previous)
                    # Created with 0666, 0600 and (mkdir) 0777, less the umask; then the folder's bits added.
                    self.assertEqual(oct(mode_of(by_path)), oct((0o666 & ~umask) | file_mode))
                    self.assertEqual(oct(mode_of(folder / "by-descriptor.json")), oct((0o600 & ~umask) | file_mode))
                    self.assertEqual(made, folder / RECORD_DIR)
                    directory_mode = (0o777 & ~umask) | folder_mode
                    if folder_mode & stat.S_ISGID and not setgid_kept(folder):
                        self.assertEqual(oct(mode_of(made) & 0o777), oct(directory_mode & 0o777))
                        self.skipTest("this system drops setgid on a directory set with chmod; the setgid bit "
                                      "of the directories is not checked")
                    self.assertEqual(oct(mode_of(made)), oct(directory_mode))
                    self.assertEqual(oct(mode_of(child)), oct(directory_mode))

    def test_bits_a_file_or_directory_already_has_are_kept(self):
        # A network mount may report the folder as 0755 or 0750 while the server gave the new file more.
        cases = ((0o755, 0o664, False, 0o664), (0o750, 0o666, False, 0o666), (0o750, 0o604, False, 0o644),
                 (0o755, 0o777, True, 0o777), (0o750, 0o705, True, 0o755))
        for folder_mode, before, directory, after in cases:
            with self.subTest(folder="{:o}".format(folder_mode), before="{:o}".format(before), directory=directory):
                folder = self.library("library-{:o}-{:o}-{}".format(folder_mode, before, directory), folder_mode)
                target = folder / "target"
                if directory:
                    target.mkdir()
                else:
                    target.write_bytes(b"")
                os.chmod(str(target), before)
                take_folder_mode(target, folder, directory=directory)
                self.assertEqual(oct(mode_of(target)), oct(after))
                if not directory:
                    os.chmod(str(target), before)
                    handle = os.open(str(target), os.O_RDONLY)
                    try:
                        take_folder_mode(handle, folder)
                    finally:
                        os.close(handle)
                    self.assertEqual(oct(mode_of(target)), oct(after))

    def test_no_chmod_is_made_when_nothing_would_be_added(self):
        folder = self.library("library", 0o755)
        target = folder / "file"
        target.write_bytes(b"")
        os.chmod(str(target), 0o664)
        made = folder / "made"
        made.mkdir()
        os.chmod(str(made), 0o775)
        with mock.patch("vidfunnel.record.os.chmod", wraps=os.chmod) as by_path, \
                mock.patch("vidfunnel.record.os.fchmod", wraps=os.fchmod) as by_descriptor:
            take_folder_mode(target, folder)
            take_folder_mode(made, folder, directory=True)
            handle = os.open(str(target), os.O_RDONLY)
            try:
                take_folder_mode(handle, folder)
            finally:
                os.close(handle)
        by_path.assert_not_called()
        by_descriptor.assert_not_called()
        self.assertEqual(oct(mode_of(target)), oct(0o664))
        self.assertEqual(oct(mode_of(made)), oct(0o775))

    def test_a_target_that_cannot_be_stated_changes_nothing(self):
        folder = self.library("library", 0o775)
        with mock.patch("vidfunnel.record.os.chmod") as by_path:
            take_folder_mode(folder / "gone", folder)
        by_path.assert_not_called()

    def test_an_existing_record_directory_is_left_as_it_is(self):
        folder = self.library("library", 0o775)
        (folder / RECORD_DIR).mkdir()
        os.chmod(str(folder / RECORD_DIR), 0o700)
        self.assertEqual(make_record_dir(folder), folder / RECORD_DIR)
        self.assertEqual(oct(mode_of(folder / RECORD_DIR)), oct(0o700))

    def test_a_file_where_the_record_directory_belongs_still_raises(self):
        folder = self.library("library", 0o775)
        (folder / RECORD_DIR).write_bytes(b"")
        with self.assertRaises(FileExistsError):
            make_record_dir(folder)

    def test_chmod_errors_are_ignored(self):
        folder = self.library("library", 0o775)
        target = folder / "file"
        target.write_bytes(b"")
        os.chmod(str(target), 0o600)
        for error in (PermissionError(errno.EPERM, "Operation not permitted"),
                      OSError(errno.ENOTSUP, "Operation not supported"),
                      OSError(errno.EROFS, "Read-only file system")):
            with self.subTest(error=error):
                with mock.patch("vidfunnel.record.os.chmod", side_effect=error) as by_path, \
                        mock.patch("vidfunnel.record.os.fchmod", side_effect=error) as by_descriptor:
                    take_folder_mode(target, folder)
                    take_folder_mode(target, folder, directory=True)
                    handle = os.open(str(target), os.O_RDONLY)
                    try:
                        take_folder_mode(handle, folder)
                    finally:
                        os.close(handle)
                self.assertEqual(by_path.call_count, 2)
                self.assertEqual(by_descriptor.call_count, 1)
                self.assertEqual(oct(mode_of(target)), oct(0o600))

    def test_a_folder_that_cannot_be_read_changes_nothing(self):
        folder = self.library("library", 0o775)
        target = folder / "file"
        target.write_bytes(b"")
        os.chmod(str(target), 0o600)
        take_folder_mode(target, folder / "gone")
        self.assertEqual(oct(mode_of(target)), oct(0o600))


# ---------------------------------------------------------------- records, lock, probe, stop file


class RecordModeTest(TempCase):

    def test_a_new_record_and_folder_file_take_the_folders_mode(self):
        folder = self.library("library", 0o775)
        source = folder / "harbour.mkv"
        set_umask(self, 0o077)
        save_record(source, {"id": "harbour"})
        save_folder(folder, {"seen": ["harbour.mkv"]})
        self.assertEqual(oct(mode_of(folder / RECORD_DIR)), oct(0o775))
        self.assertEqual(oct(mode_of(record_path(source))), oct(0o664))
        self.assertEqual(oct(mode_of(folder / RECORD_DIR / FOLDER_FILE)), oct(0o664))
        self.assertEqual(load_record(source), {"id": "harbour"})

    def test_a_record_left_owner_only_is_repaired_when_it_is_rewritten(self):
        folder = self.library("library", 0o775)
        source = folder / "harbour.mkv"
        save_record(source, {"id": "harbour"})
        os.chmod(str(record_path(source)), 0o600)
        set_umask(self, 0o077)
        save_record(source, {"id": "harbour", "status": "planned"})
        self.assertEqual(oct(mode_of(record_path(source))), oct(0o664))

    def test_the_mode_comes_from_the_library_folder_not_the_record_directory(self):
        folder = self.library("library", 0o775)
        (folder / RECORD_DIR).mkdir()
        os.chmod(str(folder / RECORD_DIR), 0o700)
        source = folder / "harbour.mkv"
        set_umask(self, 0o077)
        save_record(source, {"id": "harbour"})
        self.assertEqual(oct(mode_of(record_path(source))), oct(0o664))
        self.assertEqual(oct(mode_of(folder / RECORD_DIR)), oct(0o700))

    def test_no_temporary_file_is_left_beside_the_record(self):
        folder = self.library("library", 0o775)
        source = folder / "harbour.mkv"
        save_record(source, {"id": "harbour"})
        self.assertEqual(sorted(os.listdir(str(folder / RECORD_DIR))), ["harbour.mkv.json"])

    def test_a_record_is_never_seen_under_its_name_with_another_mode(self):
        folder = self.library("library", 0o775)
        source = folder / "harbour.mkv"
        seen = []
        real_replace = os.replace

        def replace(source_name, target_name):
            seen.append(oct(mode_of(source_name)))
            return real_replace(source_name, target_name)

        with mock.patch("vidfunnel.record.os.replace", side_effect=replace):
            save_record(source, {"id": "harbour"})
        self.assertEqual(seen, [oct(0o664)])

    def test_the_stop_file_takes_the_folders_mode(self):
        folder = self.library("library", 0o770)
        set_umask(self, 0o077)
        with contextlib.redirect_stdout(io.StringIO()):
            code = vidfunnel.stopfile.run(argparse.Namespace(paths=[str(folder)], now=False))
        self.assertEqual(code, 0)
        self.assertEqual(oct(mode_of(folder / RECORD_DIR)), oct(0o770))
        self.assertEqual(oct(mode_of(vidfunnel.stopfile.stop_path(str(folder)))), oct(0o660))

    def test_the_share_clock_probe_takes_the_folders_mode(self):
        folder = self.library("library", 0o770)
        make_record_dir(folder)
        seen = []
        real_unlink = os.unlink

        def unlink(name, *rest, **values):
            if os.path.basename(name).startswith(PROBE_PREFIX):
                seen.append(oct(mode_of(name)))
            return real_unlink(name, *rest, **values)

        with mock.patch("vidfunnel.record.os.unlink", side_effect=unlink):
            record_module._share_clock_ns(record_dir(folder))
        self.assertEqual(seen, [oct(created_mode(self.base, 0o644) | 0o660)])  # created 0644 less the umask
        self.assertEqual(os.listdir(str(record_dir(folder))), [])


class LockModeTest(TempCase):
    """The process umask is kept here (a held lock runs the heartbeat thread); the expected modes are what the
    creating call gives under it (created_mode) with the folder's bits added.

    The lock file is created with 0644. Under the usual umask 022 the 0775 subtest and the takeover test
    (0770) prove widening (group write is added: 0664); the 0700 and 0750 subtests prove that nothing is
    narrowed (the lock stays 0644 although those folders grant others nothing). Under a umask that already
    removes those bits the subtests still hold but prove less.
    """

    def test_the_lock_file_and_record_directory_follow_the_folder(self):
        for folder_mode, file_mode in ((0o775, 0o664), (0o700, 0o600), (0o750, 0o640)):
            with self.subTest(folder="{:o}".format(folder_mode)):
                folder = self.library("library-{:o}".format(folder_mode), folder_mode)
                lock_mode = created_mode(self.base, 0o644) | file_mode
                directory_mode = created_mode(self.base, 0o777) | folder_mode
                with FolderLock(folder):
                    self.assertEqual(oct(mode_of(record_dir(folder) / LOCK_NAME)), oct(lock_mode))
                    self.assertEqual(oct(mode_of(record_dir(folder))), oct(directory_mode))

    def test_a_takeover_writes_its_lock_marker_and_flock_file_with_the_folders_mode(self):
        folder = self.library("library", 0o770)
        make_record_dir(folder)
        lock = record_dir(folder) / LOCK_NAME
        started = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=25)
        lock.write_text(json.dumps({"host": "otherhost", "pid": 4242,
                                    "started": started.strftime("%Y-%m-%dT%H:%M:%SZ")}), encoding="utf-8")
        os.chmod(str(lock), 0o600)
        expected = oct(created_mode(self.base, 0o644) | 0o660)  # lock.new, marker and lock.takeover: 0644 at creation
        seen = {}

        def step(name):
            if name == "written":
                seen["marker"] = oct(mode_of(record_dir(folder) / TAKEOVER_MARKER))
                news = [entry for entry in os.listdir(str(record_dir(folder)))
                        if entry.startswith(record_module.NEW_LOCK_PREFIX)]
                seen["new"] = [oct(mode_of(record_dir(folder) / entry)) for entry in news]

        warnings = []
        with mock.patch("vidfunnel.record._takeover_step", side_effect=step):
            with FolderLock(folder, warn=warnings.append):
                self.assertEqual(oct(mode_of(lock)), expected)
                if record_module.fcntl is not None:
                    takeover_file = record_dir(folder) / record_module.TAKEOVER_FILE
                    self.assertEqual(oct(mode_of(takeover_file)), expected)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertEqual(seen, {"marker": expected, "new": [expected]})


# ---------------------------------------------------------------- the unreadable-record message


class UnreadableMessageTest(TempCase):

    def setUp(self):
        TempCase.setUp(self)
        self.folder = self.library("library", 0o775)
        self.source = self.folder / "harbour.mkv"
        save_record(self.source, {"id": "harbour"})
        self.path = record_path(self.source)

    def denied(self, number):
        real_open = open
        path = str(self.path)

        def opener(name, *rest, **values):
            if str(name) == path:
                raise PermissionError(number, os.strerror(number), path)
            return real_open(name, *rest, **values)

        return mock.patch("vidfunnel.record.open", side_effect=opener, create=True)

    def test_a_permission_error_says_who_can_fix_it(self):
        for number in (errno.EACCES, errno.EPERM):
            with self.subTest(errno=errno.errorcode[number]):
                with self.denied(number):
                    with self.assertRaises(RecordError) as caught:
                        load_record(self.source)
                message = str(caught.exception)
                self.assertTrue(message.startswith("cannot read '{}': [Errno {}]".format(self.path, number)),
                                message)
                self.assertTrue(message.endswith(
                    "; it probably belongs to another account and has owner-only permissions, which the owner "
                    "can fix with chmod on the server (chmod g+rw)"), message)

    def test_convert_names_the_record_with_the_clause(self):
        stream = io.StringIO()
        problems = []
        with self.denied(errno.EACCES):
            encode._collect([str(self.folder)], stream, problems, [])
        text = stream.getvalue()
        self.assertIn("unreadable record '{}': cannot read '{}': [Errno 13]".format(self.path, self.path), text)
        self.assertIn("it probably belongs to another account and has owner-only permissions", text)
        self.assertEqual(problems, [str(self.path)])

    def test_broken_json_keeps_its_wording(self):
        self.path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(RecordError) as caught:
            load_record(self.source)
        message = str(caught.exception)
        self.assertTrue(message.startswith("cannot read '{}': ".format(self.path)), message)
        self.assertNotIn("account", message)
        self.assertNotIn("chmod", message)

    def test_another_os_error_keeps_its_wording(self):
        real_open = open

        def opener(name, *rest, **values):
            if str(name) == str(self.path):
                raise OSError(errno.EIO, "Input/output error", str(self.path))
            return real_open(name, *rest, **values)

        with mock.patch("vidfunnel.record.open", side_effect=opener, create=True):
            with self.assertRaises(RecordError) as caught:
                load_record(self.source)
        self.assertEqual(str(caught.exception), "cannot read '{}': [Errno {}] Input/output error: '{}'".format(
            self.path, errno.EIO, self.path))


# ---------------------------------------------------------------- quarantine folders of retire


class QuarantineDirsTest(TempCase):

    def test_each_folder_retire_creates_takes_the_mode_of_the_folder_it_is_created_in(self):
        root = self.library("library", 0o770)
        (root / ".vidfunnel-retired").mkdir()
        os.chmod(str(root / ".vidfunnel-retired"), 0o750)
        set_umask(self, 0o077)
        retire._make_quarantine_dirs(root / ".vidfunnel-retired" / "show" / "season")
        retire._make_quarantine_dirs(root / "elsewhere" / "show")
        self.assertEqual(oct(mode_of(root / ".vidfunnel-retired")), oct(0o750))
        self.assertEqual(oct(mode_of(root / ".vidfunnel-retired" / "show")), oct(0o750))
        self.assertEqual(oct(mode_of(root / ".vidfunnel-retired" / "show" / "season")), oct(0o750))
        self.assertEqual(oct(mode_of(root / "elsewhere")), oct(0o770))
        self.assertEqual(oct(mode_of(root / "elsewhere" / "show")), oct(0o770))

    def test_a_file_in_the_way_still_raises(self):
        root = self.library("library", 0o770)
        (root / "blocked").write_bytes(b"")
        with self.assertRaises(FileExistsError):
            retire._make_quarantine_dirs(root / "blocked" / "show")


# ---------------------------------------------------------------- end to end


@unittest.skipIf(TOOLS is None, "ffmpeg not found on PATH; permission end-to-end tests skipped")
class EndToEndTest(TempCase):

    def setUp(self):
        TempCase.setUp(self)
        if not os.path.exists(os.path.join(FIXTURE_DIR, SUBTITLE_SOURCE)):
            self.skipTest("fixture '{}' missing; run tests/make_fixtures.sh".format(SUBTITLE_SOURCE))

    def vidfunnel(self, umask, *arguments):
        """Run 'python3 -m vidfunnel ARGUMENTS' under `umask`, set by a shell of its own."""
        environment = dict(os.environ, PYTHONPATH=REPO_ROOT)
        command = ["sh", "-c", 'umask "$1" && shift && exec "$@"', "sh", "{:03o}".format(umask),
                   sys.executable, "-m", "vidfunnel"] + list(arguments)
        result = subprocess.run(command, cwd=REPO_ROOT, env=environment, stdin=subprocess.DEVNULL,
                                capture_output=True, universal_newlines=True, timeout=300)
        self.assertEqual(result.returncode, 0, "vidfunnel {}:\n{}\n{}".format(
            " ".join(arguments), result.stdout, result.stderr))
        self.assertNotIn("Traceback", result.stderr)
        return result

    def library_with_source(self, root_mode, show_mode):
        root = self.library("library", root_mode)
        show = root / "show"
        show.mkdir()
        os.chmod(str(show), show_mode)
        shutil.copy2(os.path.join(FIXTURE_DIR, SUBTITLE_SOURCE), str(show / SUBTITLE_SOURCE))
        return root, show

    def created_files(self, show):
        files = [record_path(show / SUBTITLE_SOURCE), record_dir(show) / FOLDER_FILE, show / SUBTITLE_OUTPUT,
                 show / SUBTITLE_SIDECAR]
        return files + sorted(path for path in show.iterdir() if path.name.endswith(".par2"))

    def test_plan_convert_and_retire_under_umask_077_follow_the_folders(self):
        root, show = self.library_with_source(0o770, 0o775)
        has_par2 = shutil.which("par2") is not None
        self.vidfunnel(0o077, "plan", str(root))
        self.vidfunnel(0o077, "convert", *(["--par2", "5"] if has_par2 else []), str(root))
        self.assertEqual(load_record(show / SUBTITLE_SOURCE)["status"], "verified")
        files = self.created_files(show)
        if has_par2:
            self.assertEqual(len(files), 6, files)
        for path in files:
            self.assertEqual(oct(mode_of(path)), oct(0o664), path.name)
        self.assertEqual(oct(mode_of(record_dir(show))), oct(0o775))
        self.assertFalse(record_dir(root).exists())
        self.vidfunnel(0o077, "retire", str(root))
        quarantine = root / ".vidfunnel-retired"
        self.assertTrue((quarantine / "show" / SUBTITLE_SOURCE).is_file())
        self.assertEqual(oct(mode_of(quarantine)), oct(0o770))
        self.assertEqual(oct(mode_of(quarantine / "show")), oct(0o770))
        self.assertEqual(load_record(show / SUBTITLE_SOURCE)["status"], "retired")
        self.assertEqual(oct(mode_of(record_path(show / SUBTITLE_SOURCE))), oct(0o664))
        if not has_par2:
            self.skipTest("par2 not found on PATH; the modes of PAR2 files were not checked")

    def test_a_0755_folder_under_umask_022_changes_only_the_record(self):
        """The visible change for the usual setup: records, the folder file and the stop file (not written
        here) are 0644 instead of 0600; every other file and folder keeps the mode it had."""
        root, show = self.library_with_source(0o755, 0o755)
        has_par2 = shutil.which("par2") is not None
        self.vidfunnel(0o022, "plan", str(root))
        self.vidfunnel(0o022, "convert", *(["--par2", "5"] if has_par2 else []), str(root))
        for path in self.created_files(show):
            self.assertEqual(oct(mode_of(path)), oct(0o644), path.name)
        self.assertEqual(oct(mode_of(record_dir(show))), oct(0o755))
        if not has_par2:
            self.skipTest("par2 not found on PATH; the modes of PAR2 files were not checked")


@unittest.skipIf(TOOLS is None, "ffmpeg not found on PATH; permission end-to-end tests skipped")
class RenamedIntoPlaceTest(TempCase):
    """The output, its sidecar and PAR2 files have the folder's mode before they are renamed to their names."""

    def test_each_file_has_its_mode_when_it_is_renamed(self):
        if not os.path.exists(os.path.join(FIXTURE_DIR, SUBTITLE_SOURCE)):
            self.skipTest("fixture '{}' missing; run tests/make_fixtures.sh".format(SUBTITLE_SOURCE))
        has_par2 = shutil.which("par2") is not None
        folder = self.library("library", 0o770)
        shutil.copy2(os.path.join(FIXTURE_DIR, SUBTITLE_SOURCE), str(folder / SUBTITLE_SOURCE))
        plan_paths(TOOLS, [str(folder)], make_options(), progress=quiet())
        seen = {}
        real_replace = os.replace

        def replace(source_name, target_name, *rest, **values):
            target = Path(target_name)
            if target.parent == folder:
                seen[target.name] = oct(mode_of(source_name))
            return real_replace(source_name, target_name, *rest, **values)

        progress = quiet()
        with mock.patch("vidfunnel.encode.os.replace", side_effect=replace):
            encode.convert_paths(TOOLS, [str(folder)], convert_options(par2=5 if has_par2 else None), progress)
        self.assertEqual(load_record(folder / SUBTITLE_SOURCE)["status"], "verified", progress.stream.getvalue())
        expected = {SUBTITLE_OUTPUT, SUBTITLE_SIDECAR}
        if has_par2:
            expected.add(SUBTITLE_OUTPUT + ".par2")
        self.assertLessEqual(expected, set(seen), seen)
        # Observed with ffmpeg 7.1 and par2cmdline 1.4.0: ffmpeg creates its files with 0666 less the umask,
        # par2 with 0644 less the umask (0644 under umask 002 too); the folder adds 0660 to each.
        for name, mode in seen.items():
            created = created_mode(self.base, 0o644 if name.endswith(".par2") else 0o666)
            self.assertEqual(mode, oct(created | 0o660), name)
        if not has_par2:
            self.skipTest("par2 not found on PATH; PAR2 files were not checked")


@unittest.skipIf(TOOLS is None, "ffmpeg not found on PATH; permission end-to-end tests skipped")
class RefitRenamedIntoPlaceTest(TempCase):
    """A refit output has the folder's mode before it is renamed over the existing output."""

    def test_the_refit_output_has_its_mode_when_it_is_renamed(self):
        source_name, output_name = "h264_avi_mp3.avi", "h264_avi_mp3.mp4"
        if not os.path.exists(os.path.join(FIXTURE_DIR, source_name)):
            self.skipTest("fixture '{}' missing; run tests/make_fixtures.sh".format(source_name))
        folder = self.library("library", 0o770)
        created = created_mode(self.base, 0o666)  # ffmpeg creates the part file with 0666 less the umask
        expected = created | 0o660
        if expected == created:
            self.skipTest("under this umask ({:o} at creation) the folder adds nothing; the test would prove "
                          "nothing".format(created))
        shutil.copy2(os.path.join(FIXTURE_DIR, source_name), str(folder / source_name))
        plan_paths(TOOLS, [str(folder)], make_options(reencode_video=True), progress=quiet())
        encode.convert_paths(TOOLS, [str(folder)], convert_options(), quiet())
        self.assertIn("output-larger", load_record(folder / source_name)["flags"],
                      "the fixture no longer re-encodes larger; pick another")
        seen = []
        real_replace = os.replace

        def replace(source, target, *rest, **values):
            if Path(target) == folder / output_name:
                seen.append(oct(mode_of(source)))
            return real_replace(source, target, *rest, **values)

        progress = quiet()
        with mock.patch("vidfunnel.encode.os.replace", side_effect=replace):
            summary = encode.convert_paths(TOOLS, [str(folder)], convert_options(refit=True), progress)
        self.assertEqual(summary.counts["refitted"], 1, progress.stream.getvalue())
        self.assertEqual(seen, [oct(expected)])


class SpecialBitsTest(TempCase):
    """Directories take setgid from the folder but never sticky or setuid; files take none of the three."""

    def special_folder(self, name, mode, bit, what):
        folder = self.library(name, mode)
        if not mode_of(folder) & bit:
            self.skipTest("this system did not keep the {} bit on the test folder ({:o}); not checked".format(
                what, mode_of(folder)))
        return folder

    def test_a_sticky_library_gives_no_sticky_record_directory(self):
        folder = self.special_folder("library", 0o1777, stat.S_ISVTX, "sticky")
        set_umask(self, 0o077)
        source = folder / "harbour.mkv"
        save_record(source, {"id": "harbour"})
        # Created 0700 and 0600 under umask 077; the folder adds 0777 to the directory and 0666 to the file.
        self.assertEqual(oct(mode_of(folder / RECORD_DIR)), oct(0o777))
        self.assertEqual(oct(mode_of(record_path(source))), oct(0o666))

    def test_a_setuid_folder_gives_no_setuid_directory(self):
        folder = self.special_folder("library", 0o4775, stat.S_ISUID, "setuid")
        set_umask(self, 0o077)
        made = make_record_dir(folder)
        child = folder / "child"
        child.mkdir()
        take_folder_mode(child, folder, directory=True)
        target = folder / "file"
        target.write_bytes(b"")
        take_folder_mode(target, folder)
        self.assertEqual(oct(mode_of(made)), oct(0o775))
        self.assertEqual(oct(mode_of(child)), oct(0o775))
        self.assertEqual(oct(mode_of(target)), oct(0o664))

    def test_quarantine_folders_under_a_sticky_parent_carry_no_sticky_bit(self):
        root = self.special_folder("library", 0o1777, stat.S_ISVTX, "sticky")
        set_umask(self, 0o077)
        retire._make_quarantine_dirs(root / ".vidfunnel-retired" / "show")
        self.assertEqual(oct(mode_of(root / ".vidfunnel-retired")), oct(0o777))
        self.assertEqual(oct(mode_of(root / ".vidfunnel-retired" / "show")), oct(0o777))


@unittest.skipIf(TOOLS is None, "ffmpeg not found on PATH; permission end-to-end tests skipped")
class ChmodRefusedTest(TempCase):
    """chmod refused everywhere (a CIFS server that decides modes itself): plan and convert succeed, silently."""

    def test_plan_and_convert_succeed_when_every_chmod_fails(self):
        if not os.path.exists(os.path.join(FIXTURE_DIR, SUBTITLE_SOURCE)):
            self.skipTest("fixture '{}' missing; run tests/make_fixtures.sh".format(SUBTITLE_SOURCE))
        has_par2 = shutil.which("par2") is not None
        for number, words in ((errno.EPERM, "Operation not permitted"), (errno.ENOTSUP, "Operation not supported")):
            with self.subTest(errno=errno.errorcode[number]):
                folder = self.library("library-{}".format(errno.errorcode[number]), 0o775)
                shutil.copy2(os.path.join(FIXTURE_DIR, SUBTITLE_SOURCE), str(folder / SUBTITLE_SOURCE))
                error = PermissionError(number, words) if number == errno.EPERM else OSError(number, words)
                errors = io.StringIO()
                planned = quiet()
                converted = quiet()
                with mock.patch("vidfunnel.record.os.chmod", side_effect=error) as by_path, \
                        mock.patch("vidfunnel.record.os.fchmod", side_effect=error) as by_descriptor, \
                        contextlib.redirect_stderr(errors):
                    plan_paths(TOOLS, [str(folder)], make_options(), progress=planned)
                    summary = encode.convert_paths(TOOLS, [str(folder)], convert_options(par2=5 if has_par2 else None),
                                                   converted)
                self.assertGreater(by_path.call_count, 0)
                self.assertGreater(by_descriptor.call_count, 0)
                self.assertEqual(load_record(folder / SUBTITLE_SOURCE)["status"], "verified")
                self.assertEqual(summary.counts.get("failed", 0), 0, converted.stream.getvalue())
                self.assertTrue((folder / SUBTITLE_OUTPUT).is_file())
                self.assertTrue((folder / SUBTITLE_SIDECAR).is_file())
                text = errors.getvalue() + planned.stream.getvalue() + converted.stream.getvalue()
                for word in ("Traceback", words, "chmod"):
                    self.assertNotIn(word, text)
                self.assertEqual(oct(mode_of(record_path(folder / SUBTITLE_SOURCE))), oct(0o600))
                self.assertEqual(oct(mode_of(folder / SUBTITLE_OUTPUT)), oct(created_mode(self.base, 0o666)))


if __name__ == "__main__":
    unittest.main()

# End of File #
