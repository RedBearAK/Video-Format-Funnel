"""Tests for the natural order of work: vidfunnel.record.natural_key and the stages that use it.

The key orders names with numbers by number value ('Episode 2' before
'Episode 10'). The stage tests run in process without ffmpeg: each stage's
work on an item is a stand-in that notes the item and saves what the stage
would save, so the order in which items are started is what is compared.
With --jobs the thread pool is replaced by one that runs the tasks one after
another in the order it is given them, so no test depends on thread
scheduling; what is compared is the list order the stage hands out.
"""

VERSION = "20261111.2"

import contextlib
import datetime
import io
import json
import random
import shutil
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

import vidfunnel.interrupt
import vidfunnel.record
import vidfunnel.stopfile
from vidfunnel import encode
from vidfunnel import retire
from vidfunnel import verify
from vidfunnel.plan import iter_sources, make_options, plan_paths
from vidfunnel.record import cheap_fingerprint, iter_records, load_record, natural_key, new_record, save_record
from vidfunnel.record import set_status

# Folders and files in the order a plain sort gives them; the natural order differs in both.
SEASONS = ("Season 1", "Season 10", "Season 2")
EPISODES = ("Episode 1", "Episode 10", "Episode 2")
NATURAL_SEASONS = ("Season 1", "Season 2", "Season 10")
NATURAL_EPISODES = ("Episode 1", "Episode 2", "Episode 10")
NATURAL_ORDER = [(season, episode) for season in NATURAL_SEASONS for episode in NATURAL_EPISODES]


def natural(names):
    return sorted(names, key=natural_key)


class NaturalKeyTest(unittest.TestCase):

    def test_unpadded_numbers_sort_by_value(self):
        self.assertEqual(natural(["Episode 10.avi", "Episode 2.avi", "Episode 1.avi"]),
                         ["Episode 1.avi", "Episode 2.avi", "Episode 10.avi"])
        self.assertEqual(natural(["Season 10", "Season 2"]), ["Season 2", "Season 10"])

    def test_zero_padded_names_keep_the_plain_order(self):
        names = ["S01E10", "S02E01", "S01E02", "S01E01", "S10E01"]
        self.assertEqual(natural(names), sorted(names))

    def test_several_numbers_in_a_name(self):
        self.assertEqual(natural(["Show 2 Part 10", "Show 10 Part 1", "Show 2 Part 9", "Show 2 Part 1"]),
                         ["Show 2 Part 1", "Show 2 Part 9", "Show 2 Part 10", "Show 10 Part 1"])
        self.assertEqual(natural(["S1E10", "S1E9", "S2E1", "S1E1"]), ["S1E1", "S1E9", "S1E10", "S2E1"])

    def test_leading_digits_and_names_of_digits_only(self):
        self.assertEqual(natural(["10 Clips", "9 Clips", "100", "9", "10", "Clips"]),
                         ["9", "9 Clips", "10", "10 Clips", "100", "Clips"])

    def test_the_empty_name_comes_first(self):
        self.assertEqual(natural(["Movie", "", "1"]), ["", "1", "Movie"])
        self.assertEqual(natural_key(""), ((), ""))

    def test_equal_values_with_other_padding_are_ordered_by_the_plain_sort_of_the_names(self):
        names = ["Episode 2", "Episode 002", "Episode 02"]
        expected = ["Episode 002", "Episode 02", "Episode 2"]
        self.assertEqual(natural(names), expected)
        self.assertEqual(natural(list(reversed(names))), expected)
        self.assertEqual(natural(["9", "09", "009"]), ["009", "09", "9"])
        self.assertNotEqual(natural_key("Episode 02"), natural_key("Episode 2"))
        self.assertEqual(natural(["Episode 02 extra", "Episode 2 bonus"]), ["Episode 2 bonus", "Episode 02 extra"])

    def test_a_digit_meets_a_character_as_a_plain_sort_would_order_them(self):
        names = ["Episode a", "Episode A", "Episode :", "Episode 2", "Episode !", "Episode ", "Episode-1"]
        self.assertEqual(natural(names), sorted(names))
        self.assertEqual(natural(["Show - 1", "Show 1"]), ["Show - 1", "Show 1"])

    def test_upper_and_lower_case_still_sort_apart(self):
        self.assertEqual(natural(["apple 2", "Zoo 10", "apple 10", "Zoo 2"]),
                         ["Zoo 2", "Zoo 10", "apple 2", "apple 10"])

    def test_non_ascii_digits_are_characters(self):
        arabic_three = "٣"
        fullwidth_two, fullwidth_three = "２", "３"
        self.assertEqual(natural_key(arabic_three), (((ord(arabic_three),),), arabic_three))
        names = ["Episode " + fullwidth_three, "Episode 10", "Episode " + fullwidth_two, "Episode 9"]
        self.assertEqual(natural(names), ["Episode 9", "Episode 10", "Episode " + fullwidth_two,
                                          "Episode " + fullwidth_three])
        self.assertEqual(natural(["Episode " + fullwidth_two + "0", "Episode " + fullwidth_two + "1"]),
                         ["Episode " + fullwidth_two + "0", "Episode " + fullwidth_two + "1"])

    def test_a_surrogate_escaped_name_sorts_without_error(self):
        later = b"Episode \xff 10.avi".decode("utf-8", "surrogateescape")
        earlier = b"Episode \xff 2.avi".decode("utf-8", "surrogateescape")
        self.assertEqual(natural([later, earlier, "Episode 1.avi"]), ["Episode 1.avi", earlier, later])

    def test_a_digit_run_of_250_digits(self):
        longest = "1" * 250
        shorter = "9" * 249
        padded = "0" * 5 + "1" * 250
        self.assertEqual(natural([longest, shorter, padded, longest + "0"]), [shorter, padded, longest, longest + "0"])
        self.assertEqual(natural(["Clip " + "9" * 250 + ".avi", "Clip " + "1" + "0" * 250 + ".avi"]),
                         ["Clip " + "9" * 250 + ".avi", "Clip " + "1" + "0" * 250 + ".avi"])

    def test_generated_names_sort_the_same_from_any_starting_order(self):
        generator = random.Random(20261111)
        pieces = ["Episode", "episode", "Season", " ", "-", ".", "_", "!", ":", "Z", "a", "٣", "２",
                  b"\xfe".decode("utf-8", "surrogateescape"), ""]

        def digits():
            run = str(generator.randrange(0, 1200))
            return "0" * generator.randrange(0, 3) + run

        names = set()
        while len(names) < 300:
            parts = [generator.choice(pieces) if generator.random() < 0.6 else digits()
                     for _ in range(generator.randrange(0, 6))]
            names.add("".join(parts))
        names = sorted(names)
        reference = natural(names)
        for _round in range(5):
            shuffled = list(names)
            generator.shuffle(shuffled)
            self.assertEqual(natural(shuffled), reference)
        self.assertEqual(len({natural_key(name) for name in names}), len(names))
        # Where the natural order and the plain order disagree, both names hold ASCII digits.
        position = {name: index for index, name in enumerate(reference)}
        for first in names:
            for second in names:
                if (first < second) != (position[first] < position[second]):
                    self.assertTrue(any(c.isascii() and c.isdigit() for c in first), (first, second))
                    self.assertTrue(any(c.isascii() and c.isdigit() for c in second), (first, second))


def foreign_live_lock(folder):
    """A lock of another machine whose holder beat just now, as in test_convert_wait."""
    path = Path(folder) / ".vidfunnel" / "lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    started = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps({"host": "elsewhere-host", "pid": 4242, "started": started, "container": False,
                                "machine": "elsewhere-machine", "machine_source": "machine-id",
                                "boot": "elsewhere-boot", "flock": False,
                                "heartbeat": {"interval": vidfunnel.record.HEARTBEAT_SECONDS}}), encoding="utf-8")
    return path


def sequential_pool(jobs, tasks, on_done, sizes=None, folders=None):
    """run_pool's stand-in: each task in the order given, on this thread."""
    for index, task in enumerate(tasks):
        on_done(index, task())


class StageOrderTest(unittest.TestCase):

    def setUp(self):
        self.clean_flags()
        self.addCleanup(self.clean_flags)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "library"
        self.root.mkdir()
        self.started = []

    def clean_flags(self):
        for flag in (vidfunnel.interrupt.interrupted, vidfunnel.interrupt.draining,
                     vidfunnel.interrupt.stopped_by_file, vidfunnel.interrupt.stopped_by_signal):
            flag.clear()
        vidfunnel.interrupt.drain_tally.reset()
        vidfunnel.stopfile.reset()

    def library(self, status=None, loose=()):
        """Sources under the seasons (created in the plain order), with records of `status` when given."""
        sources = []
        for season in SEASONS:
            for episode in EPISODES:
                sources.append(self.root / season / (episode + ".avi"))
        sources.extend(self.root / (name + ".avi") for name in loose)
        for source in sources:
            source.parent.mkdir(exist_ok=True)
            source.write_bytes(("source of " + source.stem + " ").encode() * 64)
            if status is None:
                continue
            record = new_record(source)
            record["output"]["name"] = source.stem + ".mp4"
            if status == "planned":
                record["action"] = "remux"
            if status == "converted-larger":
                output = source.parent / (source.stem + ".mp4")
                output.write_bytes(("larger output of " + source.stem + " ").encode() * 96)
                record["action"] = "reencode"
                record["flags"] = ["output-larger"]
                record["output"]["fingerprint"] = cheap_fingerprint(output)
            set_status(record, "converted" if status == "converted-larger" else status)
            save_record(source, record)
        return sources

    def note(self, key):
        key = Path(key)
        self.started.append((key.parent.name, key.stem))

    def started_in_seasons(self):
        return [entry for entry in self.started if entry[0] in SEASONS]

    def common_patches(self):
        return [unittest.mock.patch("vidfunnel.plan.ffmpeg_capabilities", return_value={}),
                unittest.mock.patch("vidfunnel.plan.decodable_codecs", return_value=set()),
                unittest.mock.patch("vidfunnel.encode.run_tool", side_effect=AssertionError("ffmpeg started")),
                unittest.mock.patch("vidfunnel.record._warn")]

    def run_with(self, patches, call):
        with contextlib.ExitStack() as stack:
            for patch in self.common_patches() + patches:
                stack.enter_context(patch)
            return call()

    def run_convert(self, jobs, refit=False):
        def convert_stand_in(tools, key, record, options, progress=None, position=None, total=None):
            self.note(key)
            record = load_record(key)
            record.setdefault("stages", {})["convert"] = {"outcome": "stand-in"}
            set_status(record, "converted")
            save_record(key, record)
            return {"outcome": "ok"}

        def refit_stand_in(tools, key, record, options, progress=None, position=None, total=None):
            self.note(key)
            return "refitted", None

        options = types.SimpleNamespace(par2=None, verbose=False, jobs=jobs, dry_run=False, refit=refit)
        progress = types.SimpleNamespace(stream=io.StringIO(), quiet=True, verbose=False)
        patches = [unittest.mock.patch("vidfunnel.encode.convert_one", side_effect=convert_stand_in),
                   unittest.mock.patch("vidfunnel.encode.refit_one", side_effect=refit_stand_in),
                   unittest.mock.patch("vidfunnel.encode.run_pool", side_effect=sequential_pool)]
        return self.run_with(patches, lambda: encode.convert_paths(None, [str(self.root)], options, progress))

    def run_plan(self, jobs):
        def plan_stand_in(tools, work, found, options, folder_sources, capabilities, progress, stream, position,
                          total, record_lock=None):
            self.note(work.key)
            record = new_record(work.key)
            record["action"] = "remux"
            record["output"]["name"] = work.key.stem + ".mp4"
            set_status(record, "planned")
            save_record(work.key, record)
            return "remux", record

        progress = types.SimpleNamespace(stream=io.StringIO(), quiet=True, verbose=False)
        patches = [unittest.mock.patch("vidfunnel.plan._plan_entry", side_effect=plan_stand_in),
                   unittest.mock.patch("vidfunnel.plan.run_pool", side_effect=sequential_pool)]
        return self.run_with(patches, lambda: plan_paths(None, [str(self.root)], make_options(jobs=jobs), progress))

    def test_iter_records_walks_folders_and_records_in_natural_order(self):
        self.library("planned", loose=("Extra 10", "Extra 9"))
        found = [(folder.name, record["source"]["name"]) for folder, record in iter_records(self.root)]
        self.assertEqual(found, [("library", "Extra 9.avi"), ("library", "Extra 10.avi")]
                         + [(season, episode + ".avi") for season, episode in NATURAL_ORDER])

    def test_iter_sources_yields_folders_and_files_in_natural_order(self):
        self.library(loose=("Extra 10", "Extra 9"))
        found = [(path.parent.name, path.stem) for path in iter_sources([str(self.root)])]
        self.assertEqual(found, [("library", "Extra 9"), ("library", "Extra 10")] + NATURAL_ORDER)

    def test_convert_starts_items_in_natural_order(self):
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                shutil.rmtree(str(self.root))
                self.root.mkdir()
                self.started = []
                self.library("planned")
                summary = self.run_convert(jobs)
                self.assertEqual(summary.counts["converted"], 9, summary.counts)
                self.assertEqual(self.started, NATURAL_ORDER)

    def test_convert_refit_starts_items_in_natural_order(self):
        self.library("converted-larger")
        summary = self.run_convert(1, refit=True)
        self.assertEqual(summary.counts["refitted"], 9, summary.counts)
        self.assertEqual(self.started, NATURAL_ORDER)

    def test_plan_starts_items_in_natural_order(self):
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                shutil.rmtree(str(self.root))
                self.root.mkdir()
                self.started = []
                self.library()
                summary = self.run_plan(jobs)
                self.assertEqual(summary.counts["remux"], 9, summary.counts)
                self.assertEqual(self.started, NATURAL_ORDER)

    def test_verify_starts_items_in_natural_order(self):
        self.library("converted")

        def verify_stand_in(tools, key, record, options, progress=None):
            self.note(key)
            return {"status": "verified", "score": 95}

        args = types.SimpleNamespace(paths=[str(self.root)], stream=io.StringIO(), quiet=True)
        patches = [unittest.mock.patch("vidfunnel.deps.require", return_value=None),
                   unittest.mock.patch("vidfunnel.verify.verify_record", side_effect=verify_stand_in)]
        self.run_with(patches, lambda: verify.run(args))
        self.assertEqual(self.started, NATURAL_ORDER)

    def test_retire_collects_candidates_in_natural_order(self):
        self.library("verified")
        summary = types.SimpleNamespace(add=lambda *values: None)
        candidates = retire._collect([str(self.root)], summary, [])
        self.assertEqual([(candidate.folder.name, Path(candidate.name).stem) for candidate in candidates],
                         NATURAL_ORDER)

    def test_the_folders_a_later_round_waits_for_are_named_in_natural_order(self):
        self.library("planned")
        locks = [foreign_live_lock(self.root / season) for season in ("Season 10", "Season 2")]
        waits = []

        def scripted_wait(folders):
            waits.append([Path(folder).name for folder in folders])
            for lock in locks:
                lock.unlink()
            return None

        stream = io.StringIO()
        options = types.SimpleNamespace(par2=None, verbose=False, jobs=1, dry_run=False, refit=False)
        progress = types.SimpleNamespace(stream=stream, quiet=True, verbose=False)

        def convert_stand_in(tools, key, record, options, progress=None, position=None, total=None):
            self.note(key)
            return {"outcome": "ok"}

        patches = [unittest.mock.patch("vidfunnel.encode.convert_one", side_effect=convert_stand_in),
                   unittest.mock.patch("vidfunnel.turns.wait_for_folders", side_effect=scripted_wait)]
        self.run_with(patches, lambda: encode.convert_paths(None, [str(self.root)], options, progress))
        self.assertEqual(waits, [["Season 2", "Season 10"]])
        text = stream.getvalue()
        self.assertLess(text.index("  '{}': held by".format(self.root / "Season 2")),
                        text.index("  '{}': held by".format(self.root / "Season 10")), text)
        self.assertEqual(self.started, NATURAL_ORDER)


if __name__ == "__main__":
    unittest.main()

# End of File #
