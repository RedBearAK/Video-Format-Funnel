"""Table-driven tests for vidfunnel.redact (HANDOFF.md section 6.8).

Each table is a list of (input, expected) pairs; reading the table is reading
the behaviour.
"""

VERSION = "20261109"

import copy
import json
import random
import re
import unittest
from unittest import mock

from vidfunnel.redact import (
    Redactor,
    redact_metadata_value,
    redact_name,
    redact_path,
    redact_word,
    trie_pattern,
    truncate_to_year,
)

WORD_TABLE = [
    ("Client", "C____t"),
    ("part2", "p___#"),
    ("2019", "####"),
    ("2nd", "#__"),
    ("x", "x"),
    ("7", "#"),
    ("Éléphant", "É______t"),
    ("ÉLAN42", "É___##"),
    ("東京", "東_"),
    ("Day", "D__"),
    ("Wedd", "W__d"),
    ("Weddi", "W___i"),
    ("Wedding", "W_____g"),
    ("reception", "r_______n"),
    ("ab12", "a_##"),
    ("abc1", "a__#"),
    ("café", "c__é"),
    ("cafe\u0301", "c__e\u0301"),
    ("cafe\u0301s", "c____s"),
    ("abe\u0301", "a___"),
    ("", ""),
]

NAME_TABLE = [
    ("Client Name - Deposition 2019-03-14 part2.avi",
     "C____t N__e - D________n ####-##-## p___#.avi"),
    ("Holiday (Paris) [1998] final.mkv", "H_____y (P___s) [####] f___l.mkv"),
    ("Éléphant.mov", "É______t.mov"),
    ("1998 summer.mpg", "#### s____r.mpg"),
    ("NoExtension", "N_________n"),
    ("movie.part1.avi", "m___e.p___#.avi"),
    ("Dr. Smith interview", "D_. S___h i_______w"),
    ("archive.tar.gz", "a_____e.t__.gz"),
    ("clip.m2ts", "c__p.m2ts"),
    ("tape_03-side_B.vob", "t__e_##-s__e_B.vob"),
    (".hidden", ".h____n"),
    ("Bob's Party!.flv", "B__'s P___y!.flv"),
    ("Wedding Day reception 2004-06-12 part 2.avi", "W_____g D__ r_______n ####-##-## p__t #.avi"),
    ("Jean-Pierre and Marie-Claire.mkv", "J__n-P____e a__ M___e-C____e.mkv"),
    ("tape.v2.final cut 03.avi", "t__e.v#.f___l c__ ##.avi"),
    ("Été à Montréal 1987.mov", "É__ à M______l ####.mov"),
]

PATH_ROOTS = ("/srv/media/Family Videos",)

PATH_TABLE = [
    ("/srv/media/Family Videos/Paris 1998/Holiday.avi", "<root>/P___s ####/H_____y.avi"),
    ("/srv/media/Family Videos", "<root>"),
    ("/srv/media/Family Videos Extra/Clip.avi", "/s__/m___a/F____y V____s E___a/C__p.avi"),
    ("/home/kris/Client Name/tape 2.mpg", "/h__e/k__s/C____t N__e/t__e #.mpg"),
    ("relative/dir/movie.avi", "r______e/d__/m___e.avi"),
    ("/a/../b/./c.dv", "/a/../b/./c.dv"),
]

METADATA_TABLE = [
    ("Client Name deposition, day 2", "C____t N__e d________n, d__ #"),
    ("Smith v. Jones (2019)", "S___h v. J___s (####)"),
    ("SoundHandler", "S__________r"),
    ("", ""),
]

YEAR_TABLE = [
    (("2019-03-14T10:22:33.000000Z", "creation_time"), "2019"),
    (("Thu Mar 14 10:22:33 2019", "creation_time"), "2019"),
    (("unknown", "date"), "u_____n"),
    ((1552558953, "mtime"), 1546300800),
    ((1552558953.5, "mtime"), 1546300800.0),
    ((1552558953123456789, "mtime_ns"), 1546300800000000000),
]


class TableTests(unittest.TestCase):

    def test_words(self):
        for word, expected in WORD_TABLE:
            with self.subTest(word=word):
                self.assertEqual(redact_word(word), expected)

    def test_names(self):
        for name, expected in NAME_TABLE:
            with self.subTest(name=name):
                self.assertEqual(redact_name(name), expected)

    def test_paths(self):
        for path, expected in PATH_TABLE:
            with self.subTest(path=path):
                self.assertEqual(redact_path(path, PATH_ROOTS), expected)

    def test_folder_path_keeps_no_extension(self):
        self.assertEqual(redact_path("/data/Season.avi", last_is_file=False), "/d__a/S____n.a__")

    def test_metadata(self):
        for value, expected in METADATA_TABLE:
            with self.subTest(value=value):
                self.assertEqual(redact_metadata_value(value), expected)

    def test_years(self):
        for (value, key), expected in YEAR_TABLE:
            with self.subTest(value=value, key=key):
                result = truncate_to_year(value, key)
                self.assertEqual(result, expected)
                self.assertIs(type(result), type(expected))


class PropertyTests(unittest.TestCase):

    SEPARATORS = " _-.()[]"
    ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789éÉøß"

    def test_length_and_separators_preserved(self):
        generator = random.Random(20261102)
        for attempt in range(400):
            length = generator.randint(1, 40)
            stem = "".join(generator.choice(self.ALPHABET + self.SEPARATORS) for position in range(length))
            name = stem + generator.choice(["", ".avi", ".mkv", ".m2ts", ".part7"])
            with self.subTest(name=name, attempt=attempt):
                redacted = redact_name(name)
                self.assertEqual(len(redacted), len(name))
                for position, char in enumerate(name):
                    if char in self.SEPARATORS:
                        self.assertEqual(redacted[position], char)
                    else:
                        self.assertIn(redacted[position], ("_", "#", char))
                self.assertEqual(redact_name(name), redacted)


SOURCE_FOLDER = "/srv/media/Family Videos/Client Name"
SOURCE_PATH = SOURCE_FOLDER + "/Client Name - Deposition 2019-03-14 part2.avi"
OUTPUT_PATH = SOURCE_FOLDER + "/Client Name - Deposition 2019-03-14 part2.mp4"
REDACTED_FOLDER = "<root>/C____t N__e"
REDACTED_SOURCE = REDACTED_FOLDER + "/C____t N__e - D________n ####-##-## p___#.avi"
REDACTED_OUTPUT = REDACTED_FOLDER + "/C____t N__e - D________n ####-##-## p___#.mp4"


def sample_record():
    return {
        "id": "3f2c9a54-7d1e-4b8a-9c0f-1a2b3c4d5e6f",
        "tool": "vidfunnel",
        "tool_version": "20261102",
        "source": {
            "name": "Client Name - Deposition 2019-03-14 part2.avi",
            "folder": SOURCE_FOLDER,
            "size": 734003200,
            "fingerprint": {"size": 734003200, "mtime_ns": 1552558953123456789,
                            "head_sha256": "ab" * 32, "tail_sha256": "cd" * 32},
            "sha256": None,
        },
        "status": "planned",
        "flags": ["aspect-ambiguous"],
        "penalties": [{"code": "aspect-ambiguous", "points": 30, "reason": "no crop agreement"}],
        "score": 70,
        "action": "reencode",
        "output": {"name": "Client Name - Deposition 2019-03-14 part2.mp4", "interim_name": None,
                   "sidecars": ["Client Name - Deposition 2019-03-14 part2.srt"]},
        "stages": {
            "probe": {
                "path": SOURCE_PATH,
                "format": {"name": "avi", "duration": 3600.0,
                           "tags": {"title": "Deposition of Client Name",
                                    "encoder": "Lavf58.29.100",
                                    "creation_time": "2019-03-14T10:22:33.000000Z"}},
                "streams": [
                    {"index": 0, "type": "video", "codec": "mpeg4", "width": 720, "height": 480,
                     "title": "Camera Left",
                     "tags": {"handler_name": "Camera Left", "language": "eng"}},
                    {"index": 1, "type": "audio", "codec": "mp3",
                     "tags": {"comment": "Recorded at Smith office"}},
                ],
                "chapters": [{"start": 0.0, "end": 60.0, "title": "Opening statement"}],
            },
            "plan": {
                "argv": ["/usr/bin/ffmpeg", "-i", SOURCE_PATH, "-c:v", "libx264",
                         "-metadata", "title=Deposition of Client Name", OUTPUT_PATH],
            },
            "convert": {
                "stderr": "Input #0, avi, from '" + SOURCE_PATH + "':\n"
                          "    title           : Deposition of Client Name\n",
                "message_classes": {"[mpeg4] error in '" + SOURCE_PATH + "'": 2},
            },
        },
        "history": [],
    }


class RedactorTests(unittest.TestCase):

    def test_text_on_ffmpeg_stderr_line(self):
        redactor = Redactor(roots=["/srv/media/Family Videos"], values=[SOURCE_PATH])
        line = "[avi @ 0x55d1c2] '" + SOURCE_PATH + "': Invalid data found when processing input"
        self.assertEqual(
            redactor.text(line),
            "[avi @ 0x55d1c2] '" + REDACTED_SOURCE + "': Invalid data found when processing input")

    def test_text_longest_first_and_word_bounded(self):
        redactor = Redactor(values=["/data/Clients", "/data/Clients/Ann.avi", "Ann"])
        table = [
            ("open /data/Clients/Ann.avi now", "open /d__a/C_____s/A__.avi now"),
            ("folder /data/Clients:", "folder /d__a/C_____s:"),
            ("Ann said; Annual report", "A__ said; Annual report"),
        ]
        for text, expected in table:
            with self.subTest(text=text):
                self.assertEqual(redactor.text(text), expected)

    def test_text_rejects_non_string(self):
        with self.assertRaises(TypeError):
            Redactor().text(None)

    def test_quoted_redacts_file_names_and_paths_it_never_learned(self):
        redactor = Redactor(roots=["/srv/media"], values=["/srv/media/Known/Ann.avi"])
        table = [
            ("'Toboggan Hill.mkv' would also be converted to 'Toboggan Hill.mp4'",
             "'T______n H__l.mkv' would also be converted to 'T______n H__l.mp4'"),
            ("from '/srv/media/Party/Grandmother's cake.avi':",
             "from '<root>/P___y/G_________r's c__e.avi':"),
            ("'Ann.avi' (planned)", "'A__.avi' (planned)"),
            ("'Holiday DVD_title01' (planned)", "'H_____y D___t____##' (planned)"),
            ("'ffmpeg' exited; codec 'h264', status 'planned'", "'ffmpeg' exited; codec 'h264', status 'planned'"),
            ("the output's name 'clip.avi'", "the output's name 'c__p.avi'"),
        ]
        for text, expected in table:
            with self.subTest(text=text):
                self.assertEqual(expected, redactor.quoted(text))

    def test_record(self):
        original = sample_record()
        untouched = copy.deepcopy(original)
        redactor = Redactor(roots=["/srv/media/Family Videos"])
        redacted = redactor.record(original)
        self.assertEqual(original, untouched, "record() must not modify its argument")

        changed = [
            (redacted["source"]["name"], "C____t N__e - D________n ####-##-## p___#.avi"),
            (redacted["source"]["folder"], REDACTED_FOLDER),
            (redacted["output"]["name"], "C____t N__e - D________n ####-##-## p___#.mp4"),
            (redacted["output"]["sidecars"], ["C____t N__e - D________n ####-##-## p___#.srt"]),
            (redacted["stages"]["probe"]["path"], REDACTED_SOURCE),
            (redacted["stages"]["probe"]["format"]["tags"]["title"], "D________n o_ C____t N__e"),
            (redacted["stages"]["probe"]["streams"][0]["title"], "C____a L__t"),
            (redacted["stages"]["probe"]["streams"][0]["tags"]["handler_name"], "C____a L__t"),
            (redacted["stages"]["probe"]["streams"][1]["tags"]["comment"], "R______d a_ S___h o____e"),
            (redacted["stages"]["probe"]["chapters"][0]["title"], "O_____g s_______t"),
            (redacted["stages"]["plan"]["argv"],
             ["/usr/bin/ffmpeg", "-i", REDACTED_SOURCE, "-c:v", "libx264",
              "-metadata", "title=D________n o_ C____t N__e", REDACTED_OUTPUT]),
            (redacted["stages"]["convert"]["stderr"],
             "Input #0, avi, from '" + REDACTED_SOURCE + "':\n"
             "    title           : D________n o_ C____t N__e\n"),
            (redacted["stages"]["convert"]["message_classes"],
             {"[mpeg4] error in '" + REDACTED_SOURCE + "'": 2}),
        ]
        for actual, expected in changed:
            with self.subTest(expected=expected):
                self.assertEqual(actual, expected)

        kept = [
            ("id", redacted["id"], original["id"]),
            ("score", redacted["score"], 70),
            ("flags", redacted["flags"], ["aspect-ambiguous"]),
            ("penalties", redacted["penalties"], original["penalties"]),
            ("format name", redacted["stages"]["probe"]["format"]["name"], "avi"),
            ("video codec", redacted["stages"]["probe"]["streams"][0]["codec"], "mpeg4"),
            ("audio codec", redacted["stages"]["probe"]["streams"][1]["codec"], "mp3"),
            ("width", redacted["stages"]["probe"]["streams"][0]["width"], 720),
            ("language", redacted["stages"]["probe"]["streams"][0]["tags"]["language"], "eng"),
            ("encoder", redacted["stages"]["probe"]["format"]["tags"]["encoder"], "Lavf58.29.100"),
            ("creation_time", redacted["stages"]["probe"]["format"]["tags"]["creation_time"],
             "2019-03-14T10:22:33.000000Z"),
            ("mtime_ns", redacted["source"]["fingerprint"]["mtime_ns"], 1552558953123456789),
            ("head hash", redacted["source"]["fingerprint"]["head_sha256"], "ab" * 32),
        ]
        for label, actual, expected in kept:
            with self.subTest(kept=label):
                self.assertEqual(actual, expected)

        self.assertEqual(redactor.text("row " + SOURCE_PATH), "row " + REDACTED_SOURCE)

    def test_record_redact_dates(self):
        redacted = Redactor(redact_dates=True).record(sample_record())
        table = [
            (redacted["stages"]["probe"]["format"]["tags"]["creation_time"], "2019"),
            (redacted["source"]["fingerprint"]["mtime_ns"], 1546300800000000000),
        ]
        for actual, expected in table:
            with self.subTest(expected=expected):
                self.assertEqual(actual, expected)

    def test_record_history_is_redacted(self):
        record = sample_record()
        record["history"] = [sample_record()]
        redacted = Redactor().record(record)
        self.assertEqual(redacted["history"][0]["source"]["name"],
                         "C____t N__e - D________n ####-##-## p___#.avi")
        self.assertEqual(redacted["history"][0]["id"], record["id"])

    def test_marked_record_interim_sidecars_are_redacted(self):
        record = sample_record()
        interim = "Client Name - Deposition 2019-03-14 part2.vidfunnel.srt"
        record["output"]["interim_name"] = "Client Name - Deposition 2019-03-14 part2.vidfunnel.mp4"
        record["output"]["interim_sidecars"] = [interim]
        # Only in an argv and in stderr, where no path key names it: the record's output section must teach it.
        record["stages"]["plan"]["sidecars"] = [
            {"kind": "srt", "argv": ["/usr/bin/ffmpeg", "-i", SOURCE_PATH, "-f", "srt", SOURCE_FOLDER + "/" + interim]}]
        record["stages"]["convert"]["stderr"] += "Output #0, srt, to '" + SOURCE_FOLDER + "/" + interim + "':\n"
        redacted = Redactor(roots=["/srv/media/Family Videos"]).record(record)
        self.assertEqual(redacted["output"]["interim_sidecars"], [redact_name(interim)])
        self.assertEqual(redact_name(interim), "C____t N__e - D________n ####-##-## p___#.v_______l.srt")
        text = json.dumps(redacted)
        for word in ("Client", "Deposition", "part2"):
            self.assertNotIn(word, text)


BOUNDED = r"(?<![^\W_])(?:{})(?![^\W_])"

TRIE_WORDS = [
    "clip", "clip 1", "clip 12", "clock", "a/b/clip 1.avi", "a/b", "a", "b",
    "é", "été", "name with spaces", "name", "dot.ted.name", "x+y", "(paren)",
    "tab\there",
]

TRIE_TEXTS = [
    "clip clip 1 clip 12 clip 123 clock clocks clipboard",
    "/home/a/b/clip 1.avi and a/b/clip 12.avi, a/b/c, a b ab ba",
    "é été étés éé (é) été-été",
    "name with spaces; name with; names with spaces; a name",
    "dot.ted.name dot.ted.names dot.ted x+y x+yz (paren) ((paren)) paren",
    "tab\there tab here tab\theres\ttab\there",
    "",
    "nothing to see",
    "clip_1 _clip 1_ clip-1 clip 1.avi",
]


class TriePatternTests(unittest.TestCase):

    def test_matches_equal_the_flat_alternation_longest_first(self):
        ordered = sorted(TRIE_WORDS, key=lambda item: (-len(item), item))
        flat = re.compile(BOUNDED.format("|".join(re.escape(item) for item in ordered)))
        trie = re.compile(BOUNDED.format(trie_pattern(TRIE_WORDS)))
        for text in TRIE_TEXTS:
            with self.subTest(text=text):
                expected = [(match.span(), match.group(0)) for match in flat.finditer(text)]
                actual = [(match.span(), match.group(0)) for match in trie.finditer(text)]
                self.assertEqual(actual, expected)
        self.assertTrue(any(flat.search(text) for text in TRIE_TEXTS))

    def test_nested_words_match_the_longest(self):
        pattern = re.compile(trie_pattern(["ab", "abc", "abcd"]))
        table = [("abcd", "abcd"), ("abcx", "abc"), ("abx", "ab")]
        for text, expected in table:
            with self.subTest(text=text):
                self.assertEqual(pattern.match(text).group(0), expected)

    def test_single_word_is_its_escaped_self(self):
        for word in ("clip", "x+y", "(paren)", "dot.ted.name"):
            with self.subTest(word=word):
                self.assertEqual(trie_pattern([word]), re.escape(word))

    def test_no_words_matches_nothing(self):
        pattern = trie_pattern([])
        self.assertEqual(pattern, "(?!)")
        compiled = re.compile(pattern)
        for text in ("", "a", "clip 1"):
            with self.subTest(text=text):
                self.assertIsNone(compiled.search(text))

    def test_empty_words_are_skipped(self):
        self.assertEqual(trie_pattern([""]), "(?!)")
        self.assertEqual(trie_pattern(["", "a"]), "a")
        compiled = re.compile(trie_pattern(["", "clip"]))
        self.assertIsNone(compiled.search("nothing here"))

    def test_word_that_ends_and_branches_has_one_optional_group(self):
        self.assertEqual(trie_pattern(["clip", "clip 1", "clip2", "clock"]),
                         r"cl(?:ip(?:\ 1|2)?|ock)")
        self.assertEqual(trie_pattern(["clip", "clip 1", "clip 2", "clock"]),
                         r"cl(?:ip(?:\ (?:1|2))?|ock)")

    def test_word_of_five_thousand_characters(self):
        # One trie level per character: a recursive renderer hit Python's recursion limit
        # near 1,000 characters.
        word = "description " * 416 + "end"
        self.assertGreater(len(word), 4990)
        pattern = trie_pattern([word, word + " more"])
        self.assertEqual(re.compile(pattern).match(word + " more").group(0), word + " more")
        self.assertEqual(trie_pattern([word]), re.escape(word))


class RedactorDepthTests(unittest.TestCase):

    def test_value_of_five_thousand_characters_is_redacted(self):
        value = "A long description of the holiday " * 147 + "at the lake"
        self.assertGreater(len(value), 5000)
        redactor = Redactor(values=[value])
        text = "comment=" + value + "; done"
        self.assertEqual(redactor.text(text), "comment=" + redact_metadata_value(value) + "; done")
        # The trie itself compiled; the flat fallback would hide a renderer that cannot go this deep.
        self.assertEqual(redactor._compiled().pattern, BOUNDED.format(trie_pattern([value])))

    def test_chain_of_a_thousand_prefixes_is_redacted(self):
        # Each value is a prefix of the next: the trie nests one optional group per value,
        # deeper than Python 3.13's regex parser compiles, so there the flat alternation is used.
        values = ["P" + "a" * length for length in range(1, 1001)]
        redactor = Redactor(values=values)
        for value in (values[0], values[499], values[-1]):
            with self.subTest(length=len(value)):
                self.assertEqual(redactor.text("[" + value + "]"),
                                 "[" + redact_metadata_value(value) + "]")
        self.assertEqual(redactor.text("P " + values[-1] + "a"), "P " + values[-1] + "a")

    def test_compile_recursion_falls_back_to_the_flat_alternation(self):
        values = ["clip", "clip 1", "clock"]
        sources = []
        real_compile = re.compile

        def failing_first(source, *rest, **options):
            sources.append(source)
            if len(sources) == 1:
                raise RecursionError("maximum recursion depth exceeded")
            return real_compile(source, *rest, **options)

        redactor = Redactor(values=values)
        with mock.patch("vidfunnel.redact.re.compile", side_effect=failing_first):
            self.assertEqual(redactor.text("clip 1 and clock"),
                             redact_metadata_value("clip 1") + " and " + redact_metadata_value("clock"))
        self.assertEqual(sources, [BOUNDED.format(trie_pattern(values)),
                                   BOUNDED.format(r"clip\ 1|clock|clip")])


if __name__ == "__main__":
    unittest.main()

# End of File #
