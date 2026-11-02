"""Tests for vidfunnel.deps: platform detection, install hints, the table, and check end to end.

The end-to-end test skips with a message when '/usr/bin/ffmpeg' is absent.
"""

VERSION = "20261102"

import argparse
import contextlib
import io
import os
import shutil
import tempfile
import unittest

from vidfunnel.cli import EXIT_DEPS, EXIT_OK
from vidfunnel.deps import (
    DISTROBOX_SCRIPT,
    CheckResult,
    DependencyError,
    Item,
    check_dependencies,
    detect_platform,
    format_check,
    install_hint,
    platform_family,
    require,
    run,
)

HOST_FFMPEG = "/usr/bin/ffmpeg"
NO_HOST_FFMPEG = "'{}' is absent; integration test skipped".format(HOST_FFMPEG)

TUMBLEWEED = {"os": "linux", "id": "opensuse-tumbleweed", "like": ["opensuse", "suse"], "pretty": "openSUSE Tumbleweed"}
FEDORA = {"os": "linux", "id": "fedora", "like": [], "pretty": "Fedora Linux 43"}
TRIXIE = {"os": "linux", "id": "debian", "like": [], "pretty": "Debian GNU/Linux 13 (trixie)"}
UBUNTU = {"os": "linux", "id": "ubuntu", "like": ["debian"], "pretty": "Ubuntu 24.04"}
ARCH = {"os": "linux", "id": "arch", "like": [], "pretty": "Arch Linux"}
ALPINE = {"os": "linux", "id": "alpine", "like": [], "pretty": "Alpine Linux v3.24"}
MACOS = {"os": "macos", "id": "macos", "like": [], "pretty": "macOS 15.1"}
GENTOO = {"os": "linux", "id": "gentoo", "like": [], "pretty": "Gentoo"}


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="vidfunnel-deps-")
        self.addCleanup(shutil.rmtree, self.folder)

    def write_os_release(self, text):
        path = os.path.join(self.folder, "os-release")
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def test_tumbleweed_os_release(self):
        path = self.write_os_release(
            'NAME="openSUSE Tumbleweed"\n# VERSION="20261020"\nID="opensuse-tumbleweed"\n'
            'ID_LIKE="opensuse suse"\nPRETTY_NAME="openSUSE Tumbleweed"\n'
        )
        found = detect_platform(path, system="linux")
        self.assertEqual(found, TUMBLEWEED)
        self.assertEqual(platform_family(found), "opensuse")

    def test_unquoted_values(self):
        path = self.write_os_release("ID=fedora\nPRETTY_NAME='Fedora Linux 43'\n")
        found = detect_platform(path, system="linux")
        self.assertEqual(found["id"], "fedora")
        self.assertEqual(found["like"], [])
        self.assertEqual(found["pretty"], "Fedora Linux 43")

    def test_missing_os_release(self):
        found = detect_platform(os.path.join(self.folder, "absent"), system="linux")
        self.assertEqual(found["os"], "linux")
        self.assertEqual(platform_family(found), "unknown")

    def test_macos_and_other(self):
        self.assertEqual(detect_platform(system="darwin")["os"], "macos")
        self.assertEqual(detect_platform(system="freebsd13")["os"], "other")

    def test_families(self):
        self.assertEqual(platform_family(FEDORA), "fedora")
        self.assertEqual(platform_family(UBUNTU), "debian")
        self.assertEqual(platform_family(TRIXIE), "debian")
        self.assertEqual(platform_family(ARCH), "arch")
        self.assertEqual(platform_family(ALPINE), "alpine")
        self.assertEqual(platform_family(MACOS), "macos")
        self.assertEqual(platform_family(GENTOO), "unknown")


class InstallHintTests(unittest.TestCase):
    def test_nothing_missing_gives_empty_text(self):
        self.assertEqual(install_hint(TUMBLEWEED, []), "")

    def test_fedora_ffmpeg(self):
        text = install_hint(FEDORA, ["libx264"])
        self.assertIn("RPM Fusion", text)
        self.assertIn("dnf swap ffmpeg-free ffmpeg --allowerasing", text)

    def test_opensuse_ffmpeg_and_par2(self):
        text = install_hint(TUMBLEWEED, ["ffmpeg", "par2"])
        self.assertIn("Packman", text)
        self.assertIn("zypper dup --from packman --allow-vendor-change", text)
        self.assertIn("zypper install par2cmdline", text)

    def test_debian_arch_alpine_macos_ffmpeg(self):
        self.assertIn("apt install ffmpeg", install_hint(TRIXIE, ["ffmpeg"]))
        self.assertIn("apt install par2", install_hint(UBUNTU, ["par2"]))
        self.assertIn("pacman -S ffmpeg", install_hint(ARCH, ["ffmpeg"]))
        self.assertIn("pacman -S par2cmdline", install_hint(ARCH, ["par2"]))
        self.assertIn("apk add ffmpeg", install_hint(ALPINE, ["aac"]))
        self.assertIn("apk add par2cmdline", install_hint(ALPINE, ["par2"]))
        self.assertIn("brew install ffmpeg", install_hint(MACOS, ["ffmpeg"]))
        self.assertIn("brew install par2", install_hint(MACOS, ["par2"]))

    def test_dvdvideo_on_linux_names_the_distrobox_script(self):
        for platform in (TUMBLEWEED, FEDORA, TRIXIE, ARCH, ALPINE, GENTOO):
            text = install_hint(platform, ["dvdvideo"])
            self.assertIn(DISTROBOX_SCRIPT, text, platform["id"])
            self.assertNotIn("zypper dup", text)

    def test_dvdvideo_on_macos_does_not_name_the_distrobox_script(self):
        text = install_hint(MACOS, ["dvdvideo"])
        self.assertNotIn(DISTROBOX_SCRIPT, text)
        self.assertIn("libdvdread", text)

    def test_distro_notes_on_dvdvideo(self):
        self.assertIn("without libdvdread", install_hint(FEDORA, ["dvdvideo"]))
        self.assertIn("without libdvdread", install_hint(TUMBLEWEED, ["dvdvideo"]))
        self.assertIn("has dvdvideo", install_hint(TRIXIE, ["dvdvideo"]))

    def test_subtitle_aligners(self):
        text = install_hint(TRIXIE, ["ffsubsync", "alass"])
        self.assertIn("pip install ffsubsync", text)
        self.assertIn("alass-cli", text)

    def test_unknown_platform_still_says_what_is_needed(self):
        text = install_hint(GENTOO, ["libx264"])
        self.assertIn("libx264", text)


def sample_result(required_present=True):
    items = [
        Item("ffmpeg", True, True, "7.1.5 '/usr/bin/ffmpeg'"),
        Item("libx264", True, required_present, "encoder" if required_present else "encoder missing"),
        Item("dvdvideo", False, False, "demuxer missing"),
        Item("par2", False, True, "'/usr/bin/par2'"),
    ]
    return CheckResult(items, required_present, TUMBLEWEED, None)


class FormatCheckTests(unittest.TestCase):
    def test_table_layout(self):
        lines = format_check(sample_result()).splitlines()
        self.assertEqual(lines[0], "platform: openSUSE Tumbleweed ('opensuse-tumbleweed')")
        self.assertEqual(lines[1].split(), ["name", "need", "status", "detail"])
        self.assertTrue(set(lines[2].replace(" ", "")) == {"-"})
        self.assertEqual(lines[3].split()[:3], ["ffmpeg", "required", "present"])
        self.assertEqual(lines[5].split()[:3], ["dvdvideo", "optional", "MISSING"])
        detail_column = lines[1].index("detail")
        self.assertEqual(lines[3][detail_column:], "7.1.5 '/usr/bin/ffmpeg'")
        self.assertEqual(lines[-1], "all required items present")

    def test_plain_ascii(self):
        format_check(sample_result(False)).encode("ascii")

    def test_missing_required_is_named(self):
        self.assertEqual(format_check(sample_result(False)).splitlines()[-1], "required items missing: libx264")


class MissingFfmpegTests(unittest.TestCase):
    def test_absent_ffmpeg_is_reported_not_raised(self):
        absent = os.path.join(tempfile.gettempdir(), "vidfunnel-no-such-folder", "ffmpeg")
        result = check_dependencies(absent, platform=TUMBLEWEED)
        self.assertFalse(result.ok)
        self.assertIsNone(result.tools)
        by_name = {item.name: item for item in result.items}
        self.assertFalse(by_name["ffmpeg"].present)
        self.assertIn("'{}'".format(absent), by_name["ffmpeg"].detail)
        self.assertFalse(by_name["libx264"].present)
        self.assertFalse(by_name["ffprobe"].present)

    def test_require_raises_and_prints_hint(self):
        absent = os.path.join(tempfile.gettempdir(), "vidfunnel-no-such-folder", "ffmpeg")
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            with self.assertRaises(DependencyError):
                require(argparse.Namespace(ffmpeg=absent))
        self.assertIn("required items missing", captured.getvalue())

    def test_run_returns_exit_deps(self):
        absent = os.path.join(tempfile.gettempdir(), "vidfunnel-no-such-folder", "ffmpeg")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run(argparse.Namespace(ffmpeg=absent)), EXIT_DEPS)


@unittest.skipUnless(os.path.exists(HOST_FFMPEG), NO_HOST_FFMPEG)
class HostCheckTests(unittest.TestCase):
    def test_check_end_to_end(self):
        result = check_dependencies(HOST_FFMPEG)
        names = [item.name for item in result.items]
        for name in ("ffmpeg", "ffprobe", "libx264", "aac", "idet", "bwdif", "fieldmatch", "cropdetect",
                     "h264_metadata", "dvdvideo", "libdvdread", "par2", "mediainfo", "ffsubsync", "alass"):
            self.assertIn(name, names)
        required = {item.name for item in result.items if item.required}
        self.assertEqual(
            required,
            {"ffmpeg", "ffprobe", "libx264", "aac", "idet", "bwdif", "fieldmatch", "cropdetect", "h264_metadata"},
        )
        self.assertTrue(result.ok, format_check(result))
        self.assertEqual(result.tools.ffmpeg, HOST_FFMPEG)

    def test_run_prints_table_and_returns_ok(self):
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = run(argparse.Namespace(ffmpeg=HOST_FFMPEG))
        self.assertEqual(code, EXIT_OK)
        self.assertIn("h264_metadata", captured.getvalue())

    def test_require_returns_tools(self):
        tools = require(argparse.Namespace(ffmpeg=HOST_FFMPEG))
        self.assertEqual(tools.ffmpeg, HOST_FFMPEG)


if __name__ == "__main__":
    unittest.main()

# End of File #
