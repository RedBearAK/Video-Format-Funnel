"""Table-driven tests for vidfunnel.redact (HANDOFF.md section 6.8).

Each table is a list of (input, expected) pairs; reading the table is reading
the behaviour.
"""

VERSION = "20261108"

import copy
import random
import unittest

from vidfunnel.redact import (
    Redactor,
    redact_metadata_value,
    redact_name,
    redact_path,
    redact_word,
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


if __name__ == "__main__":
    unittest.main()

# End of File #
