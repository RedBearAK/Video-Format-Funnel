"""retire: move verified originals out of the way (HANDOFF.md section 6.5).

The only stage that ever touches an original file. For each source it checks,
in this order (GUARDS): a usable record exists; its status is 'verified'; the
source still matches the record's cheap fingerprint; the output exists; the
score is not in the 'hold' band and, without --allow-review, not in the
'review' band; the output is not larger than the source and the record does
not carry 'keep-larger'; the output's full SHA-256 matches the record (the
only expensive guard, so it runs after the cheap ones); an output under the
interim '<stem>.vidfunnel.mp4' name (section 2.4) can be renamed to its final
name; and the quarantine destination does not exist yet. The first
failed guard raises RetireRefused and nothing is moved.

Only then the original is moved into the quarantine directory (default
'<root>/.vidfunnel-retired/', relative path below the root preserved), or,
with --destroy-originals-now after typing 'destroy' (or --yes), unlinked. An
interim-named output is then renamed to its final name. The record gains a
'retire' stage and the status 'retired', and stays where it is as the audit
trail. Output files are never removed, and records that are not 'verified'
are never touched.
"""

VERSION = "20261102"

import contextlib
import os
import shutil
import sys
from pathlib import Path

from vidfunnel.progress import Progress, StageSummary, format_bytes, is_tty, say
from vidfunnel.record import (
    FolderLock,
    LockHeld,
    RecordError,
    band,
    cheap_fingerprint,
    fingerprint_matches,
    iter_records,
    load_record,
    add_stage,
    save_record,
    set_status,
    sha256_file,
)

GUARDS = (
    "no-record",
    "status-not-verified",
    "source-changed",
    "output-missing",
    "score-hold",
    "score-review",
    "output-larger",
    "output-hash-mismatch",
    "interim-rename-blocked",
    "quarantine-exists",
)

QUARANTINE_DIR_NAME = ".vidfunnel-retired"
KEEP_LARGER_FLAG = "keep-larger"
PROGRESS_THRESHOLD = 32 * 1024 * 1024     # hash and copy show live progress above this size
COPY_BLOCK = 1024 * 1024
DESTROY_WORD = "destroy"


class RetireRefused(Exception):
    """A guard failed; .reason is one of GUARDS, .detail says why in words."""

    def __init__(self, reason, detail):
        if reason not in GUARDS:
            raise ValueError("unknown retire guard '{}'".format(reason))
        Exception.__init__(self, "{}: {}".format(reason, detail))
        self.reason = reason
        self.detail = detail


# ---------------------------------------------------------------- helpers


def _plain_name(name):
    """True when name is a bare file name: no directory part, not '.' or '..'."""
    if not isinstance(name, str) or name in ("", ".", ".."):
        return False
    if "/" in name or "\\" in name or "\0" in name:
        return False
    return True


def _same_file(first, second):
    """True when both paths exist and are the same file (hard links and case folding included)."""
    try:
        return os.path.samefile(str(first), str(second))
    except OSError:
        return False


def _same_filesystem(source, destination_folder):
    """True when a rename from source into destination_folder stays on one filesystem."""
    return os.stat(str(source)).st_dev == os.stat(str(destination_folder)).st_dev


def _hash_output(path, size, progress, quiet):
    """Full SHA-256 of the output, with a live progress line when it is large."""
    if progress is not None:
        return sha256_file(path, progress=progress)
    if size is not None and size > PROGRESS_THRESHOLD:
        with Progress("hash '{}'".format(path.name), total=size, unit="B", quiet=quiet) as live:
            return sha256_file(path, progress=live)
    return sha256_file(path)


# ---------------------------------------------------------------- guards


def check_eligible(source, record, allow_review=False, progress=None, quiet=False):
    """Run the guards up to 'interim-rename-blocked' in GUARDS order; RetireRefused at the first failure.

    'quarantine-exists' depends on the destination and is checked by retire_one.
    progress: None (a Progress line is shown for outputs over 32 MiB unless
    quiet) or a Progress-like object handed to sha256_file. Returns
    {'output_path', 'final_output_path', 'band', 'sha256_verified': True}.
    """
    source = Path(source)

    # no-record: missing, malformed, or belonging to another file.
    if not isinstance(record, dict):
        raise RetireRefused("no-record", "'{}' has no record".format(source.name))
    source_section = record.get("source")
    output_section = record.get("output")
    if not isinstance(source_section, dict) or not isinstance(output_section, dict):
        raise RetireRefused("no-record", "the record of '{}' has no source or output section".format(
            source.name))
    if source_section.get("name") != source.name:
        raise RetireRefused("no-record", "the record names '{}', not '{}'".format(
            source_section.get("name"), source.name))
    output_name = output_section.get("name")
    interim_name = output_section.get("interim_name")
    if not _plain_name(output_name):
        raise RetireRefused("no-record", "the record of '{}' has no usable output name ({!r})".format(
            source.name, output_name))
    if interim_name is not None and not _plain_name(interim_name):
        raise RetireRefused("no-record", "the record of '{}' has an unusable interim name ({!r})".format(
            source.name, interim_name))

    # status-not-verified
    status = record.get("status")
    if status != "verified":
        raise RetireRefused("status-not-verified", "status is '{}', not 'verified'".format(status))
    stages = record.get("stages")
    if isinstance(stages, dict) and "retire" in stages:
        raise RetireRefused("status-not-verified",
                            "status is 'verified' but the record already has a retire stage")

    # source-changed
    if not source.is_file():
        raise RetireRefused("source-changed", "source '{}' is missing or not a regular file".format(source))
    current = cheap_fingerprint(source)
    if not fingerprint_matches(current, source_section.get("fingerprint")):
        raise RetireRefused("source-changed",
                            "source '{}' no longer matches the fingerprint taken at plan time".format(
                                source.name))

    # output-missing
    folder = source.parent
    final_output_path = folder / output_name
    output_path = folder / (interim_name if interim_name is not None else output_name)
    if not output_path.is_file():
        raise RetireRefused("output-missing", "output '{}' does not exist".format(output_path.name))
    if _same_file(output_path, source):
        raise RetireRefused("output-missing",
                            "output '{}' is the source file itself".format(output_path.name))

    actual_size = output_path.stat().st_size

    # score-hold / score-review
    score = record.get("score")
    score_band = band(score)
    if score_band is None:
        raise RetireRefused("score-hold", "the record has no score ({!r})".format(score))
    if score_band == "hold":
        raise RetireRefused("score-hold", "score {} is in the hold band (below 60)".format(score))
    if score_band == "review" and not allow_review:
        raise RetireRefused("score-review",
                            "score {} is in the review band (60-89); pass --allow-review after checking it".format(
                                score))

    # output-larger
    flags = record.get("flags") or []
    if KEEP_LARGER_FLAG in flags:
        raise RetireRefused("output-larger",
                            "the output was kept although larger than the source (--keep-larger); "
                            "such a conversion is not eligible for retirement")
    if actual_size > current["size"]:
        raise RetireRefused("output-larger", "output '{}' ({}) is larger than the source ({})".format(
            output_path.name, format_bytes(actual_size), format_bytes(current["size"])))

    # output-hash-mismatch (size first: a different size cannot have the same hash)
    expected_hash = output_section.get("sha256")
    if not isinstance(expected_hash, str) or not expected_hash:
        raise RetireRefused("output-hash-mismatch", "the record has no output SHA-256 to compare with")
    expected_size = output_section.get("size")
    if expected_size is not None and expected_size != actual_size:
        raise RetireRefused("output-hash-mismatch", "output '{}' is {} bytes, the record says {}".format(
            output_path.name, actual_size, expected_size))
    actual_hash = _hash_output(output_path, actual_size, progress, quiet)
    if actual_hash.lower() != expected_hash.lower():
        raise RetireRefused("output-hash-mismatch",
                            "SHA-256 of output '{}' is {}, the record says {}".format(
                                output_path.name, actual_hash, expected_hash))

    # interim-rename-blocked
    if interim_name is not None and interim_name != output_name:
        if os.path.lexists(str(final_output_path)) and not _same_file(final_output_path, source):
            raise RetireRefused("interim-rename-blocked",
                                "'{}' cannot be renamed to '{}': a different file has that name".format(
                                    interim_name, output_name))

    return {
        "output_path": output_path,
        "final_output_path": final_output_path,
        "band": score_band,
        "sha256_verified": True,
    }


def quarantine_destination(source, root, quarantine_dir=None):
    """Where a source goes in quarantine: <quarantine_dir or root/.vidfunnel-retired>/<path under root>.

    ValueError when the source is not under root, or is already inside the quarantine directory.
    """
    source_absolute = Path(os.path.abspath(str(source)))
    root_absolute = Path(os.path.abspath(str(root)))
    try:
        relative = source_absolute.relative_to(root_absolute)
    except ValueError:
        raise ValueError("source '{}' is not under the root '{}'".format(source_absolute, root_absolute))
    if quarantine_dir is None:
        quarantine_absolute = root_absolute / QUARANTINE_DIR_NAME
    else:
        quarantine_absolute = Path(os.path.abspath(str(quarantine_dir)))
    try:
        source_absolute.relative_to(quarantine_absolute)
    except ValueError:
        return quarantine_absolute / relative
    raise ValueError("source '{}' is already inside the quarantine directory '{}'".format(
        source_absolute, quarantine_absolute))


# ---------------------------------------------------------------- moving


def _copy_across(source, destination, quiet):
    """Copy source to destination on another filesystem through a temp name; verify; return nothing.

    The copy lands as '.<name>.vidfunnel-copy' next to the destination and is
    renamed into place only after its size and first/last MiB match the source.
    """
    temp_path = destination.parent / ".{}.vidfunnel-copy".format(destination.name)
    if os.path.lexists(str(temp_path)):
        raise OSError("temporary copy '{}' already exists; remove it after checking".format(temp_path))
    size = source.stat().st_size
    live = None
    if size > PROGRESS_THRESHOLD:
        live = Progress("copy '{}'".format(source.name), total=size, unit="B", quiet=quiet)
    try:
        done = 0
        with open(str(source), "rb") as reader, open(str(temp_path), "xb") as writer:
            while True:
                block = reader.read(COPY_BLOCK)
                if not block:
                    break
                writer.write(block)
                done += len(block)
                if live is not None:
                    live.update(done)
            writer.flush()
            os.fsync(writer.fileno())
        shutil.copystat(str(source), str(temp_path))
        if live is not None:
            live.finish()
        source_print = cheap_fingerprint(source)
        copy_print = cheap_fingerprint(temp_path)
        for key in ("size", "head_sha256", "tail_sha256"):
            if source_print[key] != copy_print[key]:
                raise OSError("copy '{}' does not match '{}' ({} differs)".format(temp_path, source, key))
        if os.path.lexists(str(destination)):
            raise OSError("quarantine destination '{}' appeared during the copy".format(destination))
        os.replace(str(temp_path), str(destination))
    except BaseException:
        if live is not None:
            live.finish()
        try:
            os.unlink(str(temp_path))
        except FileNotFoundError:
            pass
        raise


def retire_one(source, record, root, quarantine_dir=None, destroy=False, allow_review=False,
               progress=None, quiet=False):
    """Check the guards, then quarantine (or, destroy=True, unlink) one source; return the retire stage.

    destroy=True unlinks without asking: the caller (run) has already obtained
    the confirmation. After the source is gone an interim-named output is
    renamed to its final name. The record gets a 'retire' stage
    {mode, destination, method, renamed_output, score, band} and status
    'retired' and is saved; a failed rename is recorded as 'rename_error' and
    raised as OSError after the record is saved, because the source is gone.
    """
    source = Path(source)
    eligibility = check_eligible(source, record, allow_review=allow_review, progress=progress, quiet=quiet)

    destination = None
    if destroy:
        mode = "destroy"
    else:
        mode = "quarantine"
        try:
            destination = quarantine_destination(source, root, quarantine_dir)
        except ValueError as error:
            raise RetireRefused("quarantine-exists", str(error))
        if os.path.lexists(str(destination)):
            raise RetireRefused("quarantine-exists",
                                "'{}' already exists in quarantine".format(destination))

    # The guards have passed: from here on the original is touched.
    if destroy:
        os.unlink(str(source))
        method = "unlink"
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if _same_filesystem(source, destination.parent):
            if os.path.lexists(str(destination)):
                raise RetireRefused("quarantine-exists",
                                    "'{}' already exists in quarantine".format(destination))
            os.replace(str(source), str(destination))
            method = "rename"
        else:
            if not quiet:
                say("  '{}' is on another filesystem than '{}'; copying, verifying, then removing the original".format(
                    source.name, destination.parent))
            _copy_across(source, destination, quiet)
            os.unlink(str(source))
            method = "copy"

    renamed_output = False
    rename_error = None
    output_path = eligibility["output_path"]
    final_output_path = eligibility["final_output_path"]
    if output_path != final_output_path:
        try:
            if os.path.lexists(str(final_output_path)):
                raise OSError("'{}' exists".format(final_output_path))
            os.replace(str(output_path), str(final_output_path))
            renamed_output = True
        except OSError as error:
            rename_error = "renaming '{}' to '{}' failed: {}".format(
                output_path.name, final_output_path.name, error)

    stage = {
        "mode": mode,
        "destination": str(destination) if destination is not None else None,
        "method": method,
        "renamed_output": renamed_output,
        "score": record.get("score"),
        "band": eligibility["band"],
    }
    if rename_error is not None:
        stage["rename_error"] = rename_error
    add_stage(record, "retire", stage)
    set_status(record, "retired")
    save_record(source, record)
    if rename_error is not None:
        raise OSError(rename_error)
    return record["stages"]["retire"]


# ---------------------------------------------------------------- confirmation


def confirm_destroy(sources, stdin=None, stdout=None, yes=False):
    """List every file about to be unlinked; True only after the word 'destroy' (or yes=True)."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    sources = list(sources)
    if not sources:
        say("retire: nothing eligible to destroy", stdout)
        return False
    say("retire: --destroy-originals-now will permanently delete these {} original(s), "
        "with no copy kept:".format(len(sources)), stdout)
    for source in sources:
        say("  '{}'".format(source), stdout)
    if yes:
        say("retire: --yes given, deleting without asking", stdout)
        return True
    stdout.write("Type '{}' to delete them, anything else to cancel: ".format(DESTROY_WORD))
    stdout.flush()
    answer = stdin.readline()
    if answer.strip() == DESTROY_WORD:
        return True
    say("retire: cancelled, nothing deleted", stdout)
    return False


# ---------------------------------------------------------------- command line


def add_arguments(parser):
    parser.add_argument("paths", nargs="+", metavar="PATH",
                        help="folders to walk for records, or single source files")
    parser.add_argument("--allow-review", action="store_true",
                        help="also retire sources whose score is in the review band (60-89)")
    parser.add_argument("--quarantine", metavar="DIR", default=None,
                        help="quarantine directory (default: '.vidfunnel-retired' under each PATH)")
    parser.add_argument("--destroy-originals-now", action="store_true",
                        help="unlink originals instead of moving them; asks for the word 'destroy' "
                             "unless --yes is given")


class _Candidate(object):
    """One verified source to try: its folder, name, root and display name."""

    def __init__(self, folder, name, root, display):
        self.folder = folder
        self.name = name
        self.root = root
        self.display = display

    @property
    def source(self):
        return self.folder / self.name


def _error(message):
    sys.stderr.write("retire: {}\n".format(message))
    sys.stderr.flush()


def _collect(paths, summary, problems):
    """Verified candidates under every PATH, grouped later by folder; counts the others as skipped."""
    candidates = []
    for given in paths:
        path = Path(given)
        if path.is_file():
            try:
                record = load_record(path)
            except RecordError as error:
                _error(str(error))
                problems.append(str(error))
                continue
            if record is None:
                say("retire: '{}' refused (no-record): it has no record".format(path))
                summary.add("refused no-record")
                continue
            if record.get("status") != "verified":
                summary.add("skipped")
                continue
            candidates.append(_Candidate(path.parent, path.name, path.parent, path.name))
            continue
        if not path.is_dir():
            message = "'{}' does not exist".format(path)
            _error(message)
            problems.append(message)
            continue

        def on_error(record_file, error):
            _error("skipping unreadable record '{}': {}".format(record_file, error))
            problems.append(str(error))

        for folder, record in iter_records(path, on_error=on_error):
            name = (record.get("source") or {}).get("name")
            if not _plain_name(name):
                message = "record in '{}' has no usable source name ({!r})".format(folder, name)
                _error(message)
                problems.append(message)
                continue
            if record.get("status") != "verified":
                summary.add("skipped")
                continue
            source = folder / name
            display = os.path.relpath(str(source), str(path))
            candidates.append(_Candidate(folder, name, path, display))
    return candidates


def _by_folder(candidates):
    """[(folder, [candidate, ...]), ...] in first-seen order; a source named by two PATHs counts once."""
    groups = []
    index = {}
    seen = set()
    for candidate in candidates:
        identity = os.path.abspath(str(candidate.source))
        if identity in seen:
            continue
        seen.add(identity)
        key = str(candidate.folder)
        if key not in index:
            index[key] = len(groups)
            groups.append((candidate.folder, []))
        groups[index[key]][1].append(candidate)
    return groups


def _refused(prefix, candidate, refusal, summary):
    say("{} '{}' refused ({}): {}".format(prefix, candidate.display, refusal.reason, refusal.detail))
    summary.add("refused " + refusal.reason)


def _fresh_record(candidate):
    """Re-read the record under the lock; a record that changed status meanwhile is skipped."""
    return load_record(candidate.source)


def run(args):
    from vidfunnel.cli import EXIT_ERROR, EXIT_OK, EXIT_USAGE

    quiet = bool(getattr(args, "quiet", False))
    destroy = bool(getattr(args, "destroy_originals_now", False))
    yes = bool(getattr(args, "yes", False))
    allow_review = bool(getattr(args, "allow_review", False))
    quarantine_dir = getattr(args, "quarantine", None)

    if destroy and not yes and not is_tty(sys.stdin):
        _error("--destroy-originals-now needs the word 'destroy' typed on a terminal; "
               "stdin is not a terminal, so a script must pass --yes. Nothing was touched.")
        return EXIT_USAGE

    summary = StageSummary("retire", outcomes=("retired",))
    problems = []
    candidates = _collect(args.paths, summary, problems)
    groups = _by_folder(candidates)
    total = sum(len(members) for _folder, members in groups)
    position = 0

    if not destroy:
        for folder, members in groups:
            try:
                lock = FolderLock(folder)
                lock.acquire()
            except LockHeld as error:
                _error("skipping {} file(s): {}".format(len(members), error))
                summary.add("skipped (locked)", len(members))
                problems.append(str(error))
                position += len(members)
                continue
            try:
                for candidate in members:
                    position += 1
                    prefix = "[retire {}/{}]".format(position, total)
                    say("{} '{}' -> quarantine".format(prefix, candidate.display))
                    _retire_candidate(candidate, prefix, quarantine_dir, False, allow_review, quiet,
                                      summary, problems)
            finally:
                lock.release()
    else:
        with contextlib.ExitStack() as stack:
            held = []
            for folder, members in groups:
                try:
                    stack.enter_context(FolderLock(folder))
                except LockHeld as error:
                    _error("skipping {} file(s): {}".format(len(members), error))
                    summary.add("skipped (locked)", len(members))
                    problems.append(str(error))
                    position += len(members)
                    continue
                held.extend(members)
            eligible = []
            for candidate in held:
                position += 1
                prefix = "[retire {}/{}]".format(position, total)
                say("{} '{}' checking before destroy".format(prefix, candidate.display))
                try:
                    record = _fresh_record(candidate)
                    check_eligible(candidate.source, record, allow_review=allow_review, quiet=quiet)
                except RetireRefused as refusal:
                    _refused(prefix, candidate, refusal, summary)
                    continue
                except (OSError, RecordError) as error:
                    _error("'{}': {}".format(candidate.display, error))
                    summary.add("failed")
                    problems.append(str(error))
                    continue
                eligible.append((prefix, candidate))
            if eligible and confirm_destroy([str(candidate.source) for _prefix, candidate in eligible],
                                            yes=yes):
                for prefix, candidate in eligible:
                    say("{} '{}' -> destroy".format(prefix, candidate.display))
                    _retire_candidate(candidate, prefix, quarantine_dir, True, allow_review, quiet,
                                      summary, problems)
            elif eligible:
                summary.add("cancelled", len(eligible))
            elif not quiet:
                say("retire: nothing eligible to destroy")

    say(summary.format(".vidfunnel/ under each folder (kept for retired sources)"))
    return EXIT_ERROR if problems else EXIT_OK


def _retire_candidate(candidate, prefix, quarantine_dir, destroy, allow_review, quiet, summary, problems):
    """Retire one candidate under its folder lock; refusals and errors are printed and counted."""
    try:
        record = _fresh_record(candidate)
        stage = retire_one(candidate.source, record, candidate.root, quarantine_dir=quarantine_dir,
                           destroy=destroy, allow_review=allow_review, quiet=quiet)
    except RetireRefused as refusal:
        _refused(prefix, candidate, refusal, summary)
        return
    except (OSError, RecordError, ValueError) as error:
        _error("'{}': {}".format(candidate.display, error))
        summary.add("failed")
        problems.append(str(error))
        return
    if stage["mode"] == "quarantine":
        detail = "moved to '{}' ({})".format(stage["destination"], stage["method"])
    else:
        detail = "deleted"
    if stage["renamed_output"]:
        detail += "; output renamed to its final name"
    say("{} '{}' retired: {}".format(prefix, candidate.display, detail))
    summary.add("retired")

# End of File #
