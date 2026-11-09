"""Redaction of names, paths, metadata and ffmpeg text for shareable reports (HANDOFF.md section 6.8).

Every rule here is a pure function of the string: deterministic, no salt, no
state. A word that starts with a letter keeps that letter; every further letter
becomes '_' and every further digit '#', except that a word of four or more
characters also keeps its last character when that is a letter. A word that
starts with a digit is masked completely by class. Anything that is not a
letter, digit or combining mark (space, '_', '-', '.', parentheses, brackets
and any other punctuation) is a separator and is kept as it is, so length and
separator positions survive.

    Client Name - Deposition 2019-03-14 part2.avi
    C____t N__e - D________n ####-##-## p___#.avi

The Redactor class applies these rules to whole records and substitutes every
real path and metadata value it has seen inside free text (ffmpeg argv and
stderr, rendered report rows). Records on disk are never redacted; this module
only transforms copies for output.
"""

VERSION = "20261108"

import copy
import datetime
import os
import re
import unicodedata

ROOT_MARK = "<root>"

# Free-text metadata keys redacted wherever they appear as a string field
# (stream and chapter 'title' in the probe shape, tags in any dict). Every
# other key in a 'tags' dict is redacted too unless KEPT_TAG_KEYS keeps it.
METADATA_KEYS = (
    "title", "artist", "album", "album_artist", "comment", "description",
    "synopsis", "show", "episode_id", "network", "genre", "composer",
    "performer", "publisher", "copyright", "lyrics", "grouping", "location",
    "handler_name",
)

# Tag keys kept verbatim because they describe the encoding, not the content.
# Compared in lower case; Matroska statistics keys ('_STATISTICS_*') and
# per-language variants ('BPS-eng') are matched on their base name.
KEPT_TAG_KEYS = (
    "encoder", "language", "major_brand", "minor_version", "compatible_brands",
    "vendor_id", "timecode", "rotate", "duration", "bps", "number_of_frames",
    "number_of_bytes", "vidfunnel", "creation_time", "date",
)

# Record keys whose values are never touched (identifiers, codes, numbers).
KEPT_KEYS = (
    "id", "tool", "tool_version", "status", "status_at", "created", "at",
    "flags", "action", "score", "code", "points", "sha256", "head_sha256",
    "tail_sha256", "size", "codec", "codec_name", "codec_long",
    "codec_long_name", "profile", "pix_fmt", "type", "codec_type",
)

# A final '.suffix' counts as an extension only when it looks like one, so a
# name without an extension ('Dr. Smith interview') does not leak its tail.
_EXTENSION_PATTERN = re.compile(r"^(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{1,4}$")
_YEAR_AT_START = re.compile(r"^\s*(\d{4})")
_YEAR_ANYWHERE = re.compile(r"(?<!\d)(\d{4})(?!\d)")
# A match must not sit inside a longer word: no letter or digit right before or after it.
_BOUNDED = r"(?<![^\W_])(?:{})(?![^\W_])"
# A single-quoted stretch of a message, as vidfunnel writes file names into reasons and
# errors: the opening quote at the start or after a space or bracket, the closing one at
# the end or before a space, bracket or punctuation, so an apostrophe inside a name
# ("Grandmother's party.mkv") does not end it. One line at most.
_QUOTED = re.compile(r"(?<![^\s(\[,])'([^\n]+?)'(?=$|[\s:;,.!?)\]])", re.MULTILINE)
# The record key name of a DVD title (plan._record_key_name), which has no extension.
_DVD_TITLE_KEY = re.compile(r"_title\d{2}$")


def _is_word_char(char):
    return char.isalnum() or unicodedata.category(char).startswith("M")


def _is_digit_char(char):
    return char.isdigit() or (char.isnumeric() and not char.isalpha())


def _mask_char(char):
    if _is_digit_char(char):
        return "#"
    if _is_word_char(char):
        return "_"
    return char


def _is_mark(char):
    return unicodedata.category(char).startswith("M")


def redact_word(word):
    """Redact one word: keep a non-digit first character, mask the rest by class.

    A word of four or more characters (combining marks not counted) also keeps
    its last character, with any combining marks on it, when that character is
    a letter: 'Wedding' -> 'W_____g', 'Day' -> 'D__', 'part2' -> 'p___#'. A
    word starting with a digit is masked completely. Characters that are not
    letters, digits or combining marks are returned unchanged.
    """
    if not word:
        return word
    first = word[0]
    if _is_digit_char(first):
        return "".join(_mask_char(char) for char in word)
    last = len(word) - 1
    while last > 0 and _is_mark(word[last]):
        last -= 1
    characters = sum(1 for char in word if not _is_mark(char))
    if characters >= 4 and last > 0 and word[last].isalpha() and not _is_digit_char(word[last]):
        return first + "".join(_mask_char(char) for char in word[1:last]) + word[last:]
    return first + "".join(_mask_char(char) for char in word[1:])


def trie_pattern(words):
    """A regex source matching any of `words`, shaped as a trie, longest match first.

    A flat alternation 'a|b|c|...' makes the engine try every alternative at
    every position: with ten thousand names a report took minutes. In the
    trie shape the engine follows one branch per character: 'clip', 'clip 1',
    'clip2' and 'clock' give 'cl(?:ip(?:\\ 1|2)?|ock)'. At a node that ends a
    word and also continues, the continuation is tried first (a greedy
    optional group), so the longest word wins, as the sorted flat alternation
    did. Empty words are skipped. With no words the pattern is '(?!)', which
    matches nothing (an empty source would match everywhere).

    The source is built with an explicit stack, not by recursion, because a
    learned metadata value (a description) can be thousands of characters
    long, one trie level per character.
    """
    root = {}
    for word in words:
        if not word:
            continue
        node = root
        for character in word:
            node = node.setdefault(character, {})
        node[""] = {}  # end of a word

    if not root:
        return "(?!)"
    pieces = []
    # Work items: a node to render, or a string to emit as it is.
    stack = [root]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            pieces.append(item)
            continue
        ends = "" in item
        characters = sorted(key for key in item if key != "")
        if not characters:
            continue
        grouped = ends or len(characters) > 1
        work = []
        if grouped:
            work.append("(?:")
        for number, character in enumerate(characters):
            if number:
                work.append("|")
            work.append(re.escape(character))
            work.append(item[character])
        if grouped:
            work.append(")?" if ends else ")")
        stack.extend(reversed(work))
    return "".join(pieces)


def _redact_words(text):
    """Split text into words and separators, redact each word, keep separators."""
    pieces = []
    word = []
    for char in text:
        if _is_word_char(char):
            word.append(char)
            continue
        if word:
            pieces.append(redact_word("".join(word)))
            word = []
        pieces.append(char)
    if word:
        pieces.append(redact_word("".join(word)))
    return "".join(pieces)


def split_extension(name):
    """Return (stem, extension with its dot) when the name has a plausible extension.

    An extension is a final '.suffix' of one to four ASCII letters and digits
    with at least one letter, after a non-empty stem. Otherwise ('' extension)
    the whole name is redacted.
    """
    dot = name.rfind(".")
    if dot > 0 and _EXTENSION_PATTERN.match(name[dot + 1:]):
        return name[:dot], name[dot:]
    return name, ""


def redact_name(name):
    """Redact a file name word by word, keeping separators and the extension."""
    stem, extension = split_extension(name)
    return _redact_words(stem) + extension


def looks_like_file(text):
    """True for a quoted message part that names a file or folder.

    A path (it holds '/'), a name with a plausible extension, or a DVD title's
    record key name ('<folder>_title01'). Codec, status and tool names ('h264',
    'planned', 'ffmpeg') have none of these and are left alone.
    """
    return "/" in text or bool(split_extension(text)[1]) or bool(_DVD_TITLE_KEY.search(text))


def redact_metadata_value(value):
    """Redact a free-text metadata string word by word. Non-strings are returned unchanged."""
    if not isinstance(value, str):
        return value
    return _redact_words(value)


def _normalise_roots(roots):
    normalised = []
    for root in roots:
        text = os.fspath(root)
        if not text:
            continue
        stripped = text.rstrip("/")
        normalised.append(stripped)
    return sorted(set(normalised), key=lambda item: (-len(item), item))


def _split_root(path, roots):
    """Return the part of path below the longest matching root, or None."""
    for root in _normalise_roots(roots):
        if root == "":
            if path.startswith("/"):
                return path
            continue
        if path == root or path.startswith(root + "/"):
            return path[len(root):]
    return None


def redact_path(path, roots=(), last_is_file=True):
    """Redact every component of a '/'-separated path, keeping their number and the separators.

    A path at or below one of roots (matched by whole components, longest root
    first) starts with '<root>'. The last component is treated as a file name
    (extension kept) unless last_is_file is False. Empty, '.' and '..'
    components are kept.
    """
    text = os.fspath(path)
    prefix = ""
    below = _split_root(text, roots)
    if below is not None:
        prefix = ROOT_MARK
        text = below
    components = text.split("/")
    redacted = []
    last = len(components) - 1
    for position, component in enumerate(components):
        if component in ("", ".", ".."):
            redacted.append(component)
        elif position == last and last_is_file:
            redacted.append(redact_name(component))
        else:
            redacted.append(_redact_words(component))
    return prefix + "/".join(redacted)


def _date_key(key):
    lowered = key.lower()
    return (lowered in ("creation_time", "date") or "mtime" in lowered
            or lowered.endswith("_date_utc"))


def truncate_to_year(value, key=""):
    """Truncate a date to its year.

    Strings become the first four-digit year they carry ('2019-03-14T...' ->
    '2019'); a string without one is redacted like metadata. Numbers are taken as a
    Unix time (nanoseconds when key ends in '_ns', else seconds) and become the
    start of that year in UTC, in the same unit and type.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        match = _YEAR_AT_START.match(value) or _YEAR_ANYWHERE.search(value)
        if match:
            return match.group(1)
        return redact_metadata_value(value)
    if isinstance(value, (int, float)):
        scale = 1_000_000_000 if key.lower().endswith("_ns") else 1
        moment = datetime.datetime.fromtimestamp(value / scale, datetime.timezone.utc)
        year_start = datetime.datetime(moment.year, 1, 1, tzinfo=datetime.timezone.utc)
        seconds = int(year_start.timestamp())
        result = seconds * scale
        return float(result) if isinstance(value, float) else int(result)
    return value


def _tag_kept(key):
    lowered = key.lower()
    if lowered.startswith("_statistics"):
        return True
    base = lowered.split("-", 1)[0]
    return base in KEPT_TAG_KEYS


def _is_path_key(key):
    return key == "path" or key.endswith("_path")


class Redactor:
    """Redacts records and free text for one report.

    roots: top-level paths given on the command line, shown as '<root>'.
    values: further real strings to substitute in text() (a string containing
        '/' is taken as a file path, anything else as a metadata value).
    redact_dates: truncate creation_time and mtime fields to the year.

    text() substitutes only what the Redactor knows: the roots, the values
    given here, and every path, name and metadata value of each record passed
    to learn() or record(). Call record() (or learn()) on every record before
    calling text() on anything rendered from them.
    """

    def __init__(self, roots=(), values=(), redact_dates=False):
        self.roots = tuple(os.fspath(root) for root in roots)
        self.redact_dates = bool(redact_dates)
        self._substitutions = {}
        self._pattern = None
        for root in _normalise_roots(self.roots):
            if root:
                self._add(root, ROOT_MARK)
        for value in values:
            self.add_value(value)

    # Single strings.

    def name(self, name):
        return redact_name(name)

    def path(self, path, last_is_file=True):
        return redact_path(path, self.roots, last_is_file)

    def metadata(self, value):
        return redact_metadata_value(value)

    # Learning what to substitute.

    def _add(self, real, redacted):
        if not real or real == redacted:
            return
        if self._substitutions.get(real) != redacted:
            self._substitutions[real] = redacted
            self._pattern = None

    def add_path(self, path, last_is_file=True):
        """Register a real path (and, for a file, its name and folder) for text()."""
        text = os.fspath(path)
        if not text:
            return
        self._add(text, self.path(text, last_is_file))
        if last_is_file:
            folder, name = os.path.split(text)
            if name:
                self._add(name, redact_name(name))
            if folder and folder != "/":
                self._add(folder, self.path(folder, last_is_file=False))

    def add_value(self, value):
        """Register a real metadata value (or a path, when it contains '/') for text()."""
        if not isinstance(value, str) or not value.strip():
            return
        if "/" in value:
            self.add_path(value)
        else:
            self._add(value, redact_metadata_value(value))

    def _compiled(self):
        if self._pattern is None and self._substitutions:
            try:
                self._pattern = re.compile(_BOUNDED.format(trie_pattern(self._substitutions)))
            except RecursionError:
                # Python's regex parser recurses once per nested group: several hundred
                # values that are each a prefix of the next nest that deep. The flat
                # alternation, longest first, matches the same and is only slower.
                ordered = sorted(self._substitutions, key=lambda item: (-len(item), item))
                alternatives = "|".join(re.escape(item) for item in ordered)
                self._pattern = re.compile(_BOUNDED.format(alternatives))
        return self._pattern

    def text(self, text):
        """Replace every known real path and value in text by its redacted form.

        Longest first, so a file path wins over its folder; a match must not
        sit inside a longer word (no letter or digit directly before or after).
        """
        if not isinstance(text, str):
            raise TypeError("Redactor.text() takes a str, got {}".format(type(text).__name__))
        pattern = self._compiled()
        if pattern is None:
            return text
        return pattern.sub(lambda match: self._substitutions[match.group(0)], text)

    def quoted(self, text):
        """Redact every single-quoted file name or path in text, learned or not.

        A reason or error can name a file that no collected record holds (the
        other source of an output collision, removed since, or outside the
        PATH), which text() cannot know. Each quoted part that looks_like_file()
        is replaced by its learned redacted form, else redacted as a path (when
        it holds '/') or a name. Call it on real text, before text().
        """
        if not isinstance(text, str):
            raise TypeError("Redactor.quoted() takes a str, got {}".format(type(text).__name__))

        def replace(match):
            inner = match.group(1)
            if not looks_like_file(inner):
                return match.group(0)
            if inner in self._substitutions:
                redacted = self._substitutions[inner]
            elif "/" in inner:
                redacted = self.path(inner, last_is_file=not inner.endswith("/"))
            else:
                redacted = redact_name(inner)
            return "'" + redacted + "'"

        return _QUOTED.sub(replace, text)

    # Records.

    def learn(self, record):
        """Register the paths, names and metadata values of a record (and its history)."""
        source = record.get("source") or {}
        output = record.get("output") or {}
        folder = source.get("folder")
        if isinstance(folder, str) and folder:
            self.add_path(folder, last_is_file=False)
        for holder, keys in ((source, ("name",)), (output, ("name", "interim_name"))):
            for key in keys:
                name = holder.get(key)
                if isinstance(name, str) and name:
                    if isinstance(folder, str) and folder:
                        self.add_path(os.path.join(folder, name))
                    else:
                        self.add_path(name)
        for sidecar in output.get("sidecars") or ():
            sidecar_name = sidecar.get("name") if isinstance(sidecar, dict) else sidecar
            if isinstance(sidecar_name, str) and sidecar_name:
                if isinstance(folder, str) and folder:
                    self.add_path(os.path.join(folder, sidecar_name))
                else:
                    self.add_path(sidecar_name)
        self._learn_walk(record)
        for previous in record.get("history") or ():
            if isinstance(previous, dict):
                self.learn(previous)

    def _learn_walk(self, value, key=""):
        if isinstance(value, dict):
            for child_key, child in value.items():
                if child_key == "history":
                    continue
                if child_key == "tags" and isinstance(child, dict):
                    for tag_key, tag_value in child.items():
                        if not _tag_kept(str(tag_key)):
                            self.add_value(tag_value)
                    continue
                self._learn_walk(child, str(child_key))
        elif isinstance(value, (list, tuple)):
            for child in value:
                self._learn_walk(child, key)
        elif isinstance(value, str):
            if key in METADATA_KEYS:
                self.add_value(value)
            elif _is_path_key(key) and value:
                self.add_path(value)

    def record(self, record):
        """Return a redacted deep copy of a record; the argument is not modified.

        Names and paths are redacted, every free-text metadata value too, and
        the remaining strings pass through text(). Identifiers, codes, numbers,
        flags, scores and kept tags such as 'encoder' are left alone; dates are
        truncated to the year only when redact_dates is set.
        """
        if not isinstance(record, dict):
            raise TypeError("Redactor.record() takes a dict, got {}".format(type(record).__name__))
        self.learn(record)
        return self._record(record)

    def redacted(self, record):
        """Return a redacted deep copy of a record already passed to learn(); learns nothing.

        For many records, call learn() on all of them first and then redacted()
        on each: every learn() invalidates the compiled pattern, so learning and
        redacting record by record recompiles an ever longer alternation for
        each record (minutes for a few thousand records); learning first
        compiles it once.
        """
        if not isinstance(record, dict):
            raise TypeError("Redactor.redacted() takes a dict, got {}".format(type(record).__name__))
        return self._record(record)

    def _record(self, record):
        result = {}
        for key, value in record.items():
            if key == "source" and isinstance(value, dict):
                result[key] = self._file_section(value)
            elif key == "output" and isinstance(value, dict):
                result[key] = self._file_section(value)
            elif key == "history" and isinstance(value, list):
                result[key] = [self._record(item) if isinstance(item, dict) else self._walk(item, key)
                               for item in value]
            else:
                result[key] = self._walk(value, key)
        return result

    def _file_section(self, section):
        result = {}
        for key, value in section.items():
            if key in ("name", "interim_name") and isinstance(value, str):
                result[key] = redact_name(value)
            elif key == "folder" and isinstance(value, str):
                result[key] = self.path(value, last_is_file=False)
            elif key == "sidecars" and isinstance(value, list):
                result[key] = [self._sidecar(item) for item in value]
            else:
                result[key] = self._walk(value, key)
        return result

    def _sidecar(self, sidecar):
        if isinstance(sidecar, str):
            return redact_name(sidecar)
        if isinstance(sidecar, dict):
            return {key: (redact_name(value) if key == "name" and isinstance(value, str)
                          else self._walk(value, key))
                    for key, value in sidecar.items()}
        return self._walk(sidecar)

    def _key(self, key):
        if not isinstance(key, str):
            return key
        if key in self._substitutions:
            return self._substitutions[key]
        if any(char.isspace() or char == "/" for char in key):
            return self.text(key)
        return key

    def _tags(self, tags):
        result = {}
        for key, value in tags.items():
            key_text = str(key)
            if self.redact_dates and _date_key(key_text):
                result[key] = truncate_to_year(value, key_text)
            elif _tag_kept(key_text):
                result[key] = copy.deepcopy(value)
            elif isinstance(value, str):
                result[key] = redact_metadata_value(value)
            else:
                result[key] = self._walk(value, key_text)
        return result

    def _walk(self, value, key=""):
        if key in KEPT_KEYS:
            return copy.deepcopy(value)
        if self.redact_dates and key and _date_key(key):
            if isinstance(value, (str, int, float)):
                return truncate_to_year(value, key)
        if isinstance(value, dict):
            if key == "tags":
                return self._tags(value)
            return {self._key(child_key): self._walk(child, str(child_key))
                    for child_key, child in value.items()}
        if isinstance(value, list):
            return [self._walk(child, key) for child in value]
        if isinstance(value, tuple):
            return tuple(self._walk(child, key) for child in value)
        if isinstance(value, str):
            if key in METADATA_KEYS:
                return redact_metadata_value(value)
            if _is_path_key(key):
                return self.path(value)
            if key == "folder":
                return self.path(value, last_is_file=False)
            return self.text(value)
        return copy.deepcopy(value)

# End of File #
