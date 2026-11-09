"""The 'report' subcommand: a sorted table built from the records (HANDOFF.md sections 6.6 and 6.8).

A report only reads records; it never probes, encodes or writes anything but its
output. Names, paths, metadata and log text are redacted by default through
vidfunnel.redact.Redactor; --reveal turns that off. Record IDs are always shown,
so any row can be resolved locally with 'vidfunnel find ID'.

Sort order (HANDOFF section 5, "worst at the top"): scored rows first, score
ascending, then the rows without a score (planned or failed records that verify
never scored), then status in the order of record.STATUSES, then path, then ID.

Every report starts with a header: report ID, generation time, tool version,
the invocation, the redaction state and a hint line. Markdown carries the
header both as a readable list and as one machine-readable HTML comment, and
JSON carries it under "header", so 'report --from FILE' can read the invocation
and each row's status and score back. CSV is plain RFC 4180 data (one column
name row, then one row per record); its header goes to stderr and it cannot be
used with --from.

The text format (the default) is for reading in a terminal: the header as
plain lines, then the rows grouped by folder, the folder line printed once per
group. Groups are ordered by the position of their worst row in the sort order
below, and rows inside a group keep that order, so the worst row of the whole
report is still the first row printed. It cannot be read back with --from.

A quarantined or failed row carries a one-line 'reason' (see _reason): a
'reason:' line under the row in text, a Reasons section after the table in
Markdown (which --from skips, as it skips everything but the table and the
Folders section), a 'reason' field in JSON and a 'reason' column in CSV.

Text and Markdown show each record ID as its first 8 characters (longer only
when two of the records collected, before any filter, would share them), which
'vidfunnel find' accepts as a prefix. '--from' resolves such prefixes against the records it finds; a
prefix that matches more than one record is an error naming both. JSON and CSV
keep the full IDs.

A redacted report records its paths as '<root>', which is what keeps the
directory structure out of it. '--from' on such a report therefore needs the
PATH arguments again, or the central index (rows are then found by record ID).
A revealed Markdown report made from the index is re-run from the whole index
when it is usable; its Folders section is read only when it is not.
"""

VERSION = "20261108"

import argparse
import bisect
import csv
import io
import json
import os
import re
import shlex
import sys
import uuid
from pathlib import Path

import vidfunnel
from vidfunnel.progress import format_bytes, say
from vidfunnel.record import STATUSES, STAGES, RecordError, band, iter_records, load_record, now_iso
from vidfunnel.redact import ROOT_MARK, Redactor, redact_path

FORMATS = ("text", "md", "json", "csv")
DEFAULT_FORMAT = "text"

HINT_REDACTED = (
    "Names in this report are redacted. To see them, re-run the same command with --reveal, "
    "or vidfunnel report --from <this file> --reveal. To locate one row: vidfunnel find <ID>."
)
# Text and CSV reports cannot be read back with --from, so their hint does not offer it.
HINT_REDACTED_NO_FROM = (
    "Names in this report are redacted. To see them, re-run the same command with --reveal; "
    "for a report that --from can read back, use --format md. To locate one row: vidfunnel find <ID>."
)
FORMATS_WITHOUT_FROM = ("text", "csv")
HINT_REVEALED = (
    "Names in this report are shown (--reveal); do not share it as it is. "
    "To locate one row: vidfunnel find <ID>."
)

HEADER_COMMENT_START = "<!-- vidfunnel-report-header "
HEADER_COMMENT_END = " -->"

# Row fields, in CSV column order. 'logs' is added only with --with-logs.
ROW_FIELDS = (
    "id", "score", "band", "status", "action", "flags", "quarantine_reason",
    "source_size", "output_size", "ratio", "path", "output_name", "changed_since", "reason",
)
LOG_KEYS = ("stderr", "log", "run_log")
MD_COLUMNS = ("ID", "score", "band", "status", "action", "flags", "source", "size")
# The table of reports written before 20261108; --from still reads it.
OLD_MD_COLUMNS = ("ID", "score", "band", "status", "action", "flags", "source -> output", "path")
SHORT_ID_LENGTH = 8
# A row's real folder as a number (rows_from_records); not part of the JSON or CSV output.
FOLDER_GROUP_KEY = "_folder_group"
FOLDERS_HEADING = "## Folders"
REASONS_HEADING = "## Reasons"
TEXT_TITLE = "vidfunnel report"



class ReportError(Exception):
    """A report cannot be produced or read back; the message says why."""


class ReportUsageError(ReportError):
    """The report given to --from is of a kind that cannot be read back (a text report)."""


# Above this many records rows_from_records says it is redacting (about 0.6 s per thousand).
REDACTION_NOTICE_RECORDS = 500


def _stderr(message, args=None):
    if args is not None and getattr(args, "quiet", False):
        return
    say(message, stream=sys.stderr)


# ---------------------------------------------------------------- collecting


def roots_for(paths):
    """Absolute folder paths used as '<root>': a folder as given, a file's parent folder."""
    roots = []
    for path in paths:
        absolute = Path(path).absolute()
        root = absolute if absolute.is_dir() or not absolute.exists() else absolute.parent
        text = str(root)
        if text not in roots:
            roots.append(text)
    return roots


def _from_index(paths, db_path):
    """(folder, record) pairs from the central index, or None when it cannot be used."""
    try:
        from vidfunnel import index as index_module
    except ImportError:
        _stderr("report: the index module is not available; reading the records instead")
        return None
    location = db_path if db_path else index_module.default_db_path()
    if not os.path.isfile(str(location)):
        _stderr("report: no index at '{}'; reading the records instead".format(location))
        return None
    connection = index_module.open_index(location)
    try:
        if paths:
            rows = []
            for path in paths:
                rows.extend(index_module.query(connection, folder_prefix=str(Path(path).absolute())))
        else:
            rows = index_module.query(connection)
    finally:
        close = getattr(connection, "close", None)
        if close is not None:
            close()
    items = []
    for row in rows:
        try:
            record = row.get("record")
            if not isinstance(record, dict):
                record = json.loads(row["json"])
        except (KeyError, TypeError, ValueError) as error:
            _stderr("report: skipping an unreadable index row: {}".format(error))
            continue
        if isinstance(record, dict):
            items.append((Path(row["folder"]), record))
    return items


def collect(paths, use_index=False, db_path=None, errors=None):
    """Return (folder, record) pairs for every record under paths, each record ID once.

    A PATH that is a folder is walked with record.iter_records; a PATH that is a
    file contributes that file's own record. use_index reads the central index
    instead (vidfunnel.index), falling back to the records when the index module
    or database is missing. Unreadable records are reported on stderr, skipped
    and appended to errors (a list) when one is given.
    """
    items = None
    if use_index:
        items = _from_index(paths, db_path)
    if items is None:
        if not paths:
            raise ReportError("no PATH given and no usable index: nothing to read")
        items = []

        def on_error(path, error):
            _stderr("report: skipping unreadable record '{}': {}".format(path, error))
            if errors is not None:
                errors.append(str(path))

        for path in paths:
            location = Path(path)
            if location.is_dir():
                items.extend(iter_records(location, on_error=on_error))
            elif location.is_file():
                try:
                    record = load_record(location)
                except RecordError as error:
                    on_error(location, error)
                    continue
                if record is not None:
                    items.append((location.absolute().parent, record))
            else:
                raise ReportError("path '{}' does not exist".format(path))
    seen = set()
    unique = []
    for folder, record in items:
        record_id = record.get("id")
        if record_id in seen:
            continue
        seen.add(record_id)
        unique.append((folder, record))
    return unique


def filter_items(items, min_score=None, max_score=None, statuses=None):
    """Keep records within the score range and status list. A score filter drops unscored records."""
    kept = []
    for folder, record in items:
        score = record.get("score")
        if min_score is not None and (not isinstance(score, (int, float)) or score < min_score):
            continue
        if max_score is not None and (not isinstance(score, (int, float)) or score > max_score):
            continue
        if statuses and record.get("status") not in statuses:
            continue
        kept.append((folder, record))
    return kept


# ---------------------------------------------------------------- rows




def _logs(record):
    stages = record.get("stages") or {}
    order = [name for name in STAGES if name in stages]
    order.extend(sorted(name for name in stages if name not in STAGES))
    entries = []
    for stage in order:
        section = stages.get(stage)
        if not isinstance(section, dict):
            continue
        for key in LOG_KEYS:
            value = section.get(key)
            if isinstance(value, list):
                value = "\n".join(str(line) for line in value)
            if isinstance(value, str) and value.strip():
                entries.append({"stage": stage, "kind": key, "text": value})
    return entries


def _first_line(text):
    """The first non-empty line of text, with ' ...' when more non-empty lines follow; None for none."""
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    if not lines:
        return None
    return lines[0] + (" ..." if len(lines) > 1 else "")


def _reason(record):
    """Why a quarantined or failed record is so, on one line; None for any other status.

    Quarantined: the plan stage's collision_reason (set when a later source's plan
    quarantined this planned record, whose plan reason is still its planning reason),
    else the plan stage's reason when its action is 'quarantine', prefixed with the
    quarantine_reason code when the text does not start with it; the code alone when
    neither is stored. Failed: the 'error' of the last stage (in record.STAGES order,
    unknown stages after) that stored one, prefixed with the stage name, as its first
    line plus ' ...' when it has more; else the first line of that stage's stderr.
    """
    status = record.get("status")
    stages = record.get("stages") or {}
    if status == "quarantined":
        code = record.get("quarantine_reason")
        plan = stages.get("plan") if isinstance(stages.get("plan"), dict) else {}
        text = plan.get("collision_reason")
        if not (isinstance(text, str) and text.strip()):
            text = plan.get("reason") if plan.get("action") == "quarantine" else None
        if not (isinstance(text, str) and text.strip()):
            return _one_line(code) if code else None
        text = _one_line(text).strip()
        if code and not text.startswith(str(code)):
            text = "{}: {}".format(code, text)
        return text
    if status == "failed":
        order = [name for name in STAGES if name in stages]
        order.extend(sorted(name for name in stages if name not in STAGES))
        for stage in reversed(order):
            section = stages.get(stage)
            if not isinstance(section, dict):
                continue
            for key in ("error", "stderr"):
                value = section.get(key)
                if isinstance(value, list):
                    value = "\n".join(str(line) for line in value)
                if isinstance(value, str) and value.strip():
                    return "{}: {}".format(stage, _first_line(value))
    return None


def rows_from_records(items, redactor=None, with_logs=False, args=None):
    """One row dict per (folder, record); with a redactor, rows come from redacted copies.

    Every record is passed to redactor.learn() before any is redacted, so the
    redactor compiles its pattern once with every real name in it; learning
    and redacting record by record recompiled an ever longer pattern for each
    record and took minutes for a few thousand records. Redaction takes about
    0.6 s per thousand records, so above REDACTION_NOTICE_RECORDS records a line
    on stderr says it is running (silenced by --quiet through args).
    """
    prepared = []
    groups = []
    group_numbers = {}
    if redactor is not None:
        if len(items) > REDACTION_NOTICE_RECORDS:
            _stderr("report: redacting {} records".format(len(items)), args)
        for _folder, record in items:
            redactor.learn(record)
    for folder, record in items:
        # Rows are grouped by the real folder, which redaction may make look like
        # another one ('Christmas 1998' and 'Christmas 2001'); the row carries only
        # a number for it, so no real path reaches a redacted report.
        real_folder = str((record.get("source") or {}).get("folder") or "")
        groups.append(group_numbers.setdefault(real_folder, len(group_numbers)))
        if redactor is not None:
            prepared.append(redactor.redacted(record))
        else:
            prepared.append(record)

    def clean(value):
        if redactor is not None and isinstance(value, str):
            return redactor.text(value)
        return value

    rows = []
    for record, group in zip(prepared, groups):
        source = record.get("source") or {}
        output = record.get("output") or {}
        folder = source.get("folder") or ""
        name = source.get("name") or ""
        if folder and name:
            path = folder.rstrip("/") + "/" + name if folder != "/" else "/" + name
        else:
            path = folder or name
        source_size = source.get("size")
        output_size = output.get("size")
        ratio = None
        if isinstance(source_size, int) and isinstance(output_size, int) and source_size > 0:
            ratio = round(float(output_size) / source_size, 3)
        # The name the output has or will have: the interim name ('<stem>.vidfunnel.mp4') until
        # retire renames it, and after a retire whose rename failed (renamed_output false).
        output_name = output.get("name")
        retire_stage = (record.get("stages") or {}).get("retire")
        renamed = isinstance(retire_stage, dict) and retire_stage.get("renamed_output") is True
        if output.get("interim_name") and not renamed:
            output_name = output.get("interim_name")
        elif not output_name:
            output_name = output.get("interim_name")
        score = record.get("score")
        row = {
            "id": record.get("id"),
            "score": score,
            "band": band(score),
            "status": record.get("status"),
            "action": record.get("action"),
            "flags": [clean(str(flag)) for flag in record.get("flags") or ()],
            "quarantine_reason": clean(record.get("quarantine_reason")),
            "source_size": source_size,
            "output_size": output_size,
            "ratio": ratio,
            "path": clean(path),
            "output_name": clean(output_name),
            "changed_since": None,
            "reason": clean(_reason(record)),
            FOLDER_GROUP_KEY: group,
        }
        if with_logs:
            row["logs"] = [dict(entry, text=clean(entry["text"])) for entry in _logs(record)]
        rows.append(row)
    return rows


def _status_rank(status):
    try:
        return STATUSES.index(status)
    except ValueError:
        return len(STATUSES)


def sort_rows(rows):
    """Scored rows first, ascending (worst conversions at the top), then unscored rows, status, path, ID.

    Unscored rows are records no conversion has been verified for (planned, failed);
    they go below the conversions the report exists to rank (HANDOFF 6.6).
    """
    def key(row):
        score = row.get("score")
        scored = isinstance(score, (int, float)) and not isinstance(score, bool)
        return (0 if scored else 1, score if scored else 0, _status_rank(row.get("status")),
                str(row.get("status") or ""), str(row.get("path") or ""), str(row.get("id") or ""))
    return sorted(rows, key=key)


def mark_changed(rows, stored_rows):
    """Set 'changed_since' on rows whose status or score differs from a stored report's row.

    stored_rows: {id: {'status': ..., 'score': ...}} from parse_report_header().
    Returns the IDs of the stored report that have no row now.
    """
    present = set()
    for row in rows:
        row_id = row.get("id")
        present.add(row_id)
        if row_id not in stored_rows:
            row["changed_since"] = "not in the earlier report"
            continue
        earlier = stored_rows[row_id]
        changes = []
        if earlier.get("status") != row.get("status"):
            changes.append("status {} -> {}".format(earlier.get("status") or "--", row.get("status") or "--"))
        if earlier.get("score") != row.get("score"):
            changes.append("score {} -> {}".format(_score_text(earlier.get("score")),
                                                   _score_text(row.get("score"))))
        if changes:
            row["changed_since"] = "; ".join(changes)
    return [row_id for row_id in stored_rows if row_id not in present]


# ---------------------------------------------------------------- header


def invocation_from_args(args, redactor=None):
    """The argv of this report ('vidfunnel', 'report', ...); paths redacted when a redactor is given."""
    argv = ["vidfunnel", "report", "--format", getattr(args, "format", None) or DEFAULT_FORMAT]
    if getattr(args, "min_score", None) is not None:
        argv.extend(["--min-score", str(args.min_score)])
    if getattr(args, "max_score", None) is not None:
        argv.extend(["--max-score", str(args.max_score)])
    for status in getattr(args, "status", None) or ():
        argv.extend(["--status", status])
    for flag, attribute in (("--reveal", "reveal"), ("--redact-dates", "redact_dates"),
                            ("--with-logs", "with_logs"), ("--no-index", "no_index")):
        if getattr(args, attribute, False):
            argv.append(flag)
    db_path = getattr(args, "db", None)
    if db_path:
        argv.extend(["--db", redact_path(str(db_path)) if redactor is not None else str(db_path)])
    paths = [str(Path(path).absolute()) for path in getattr(args, "paths", None) or ()]
    if paths:
        argv.append("--")
        for path in paths:
            argv.append(redactor.path(path, last_is_file=not os.path.isdir(path))
                        if redactor is not None else path)
    return argv


def build_header(args, records_count, redactor=None, from_report=None, unreadable=0):
    """The report header: ID, time, tool version, invocation, redaction state, hint."""
    redacted = not getattr(args, "reveal", False)
    form = getattr(args, "format", None) or DEFAULT_FORMAT
    hint = HINT_REVEALED
    if redacted:
        hint = HINT_REDACTED_NO_FROM if form in FORMATS_WITHOUT_FROM else HINT_REDACTED
    if redacted and redactor is None:
        redactor = Redactor(roots=roots_for(getattr(args, "paths", None) or ()))
    invocation = invocation_from_args(args, redactor if redacted else None)
    header = {
        "report_id": str(uuid.uuid4()),
        "generated_at": now_iso(),
        "tool": "vidfunnel",
        "tool_version": vidfunnel.VERSION,
        "report_version": VERSION,
        "invocation": invocation,
        "command": " ".join(shlex.quote(part) for part in invocation),
        "format": form,
        "redacted": redacted,
        "redact_dates": bool(getattr(args, "redact_dates", False)),
        "with_logs": bool(getattr(args, "with_logs", False)),
        "records": records_count,
        "unreadable_records": unreadable,
        "hint": hint,
    }
    if from_report is not None:
        header["from_report"] = from_report
    return header


# ---------------------------------------------------------------- rendering


def _score_text(score):
    return "--" if score is None else str(score)


def _cell(value):
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _fence(text):
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _common_prefix_length(first, second):
    length = 0
    for left, right in zip(first, second):
        if left != right:
            break
        length += 1
    return length


def short_ids(rows):
    """{full ID: shown ID}: the first 8 characters, more where two of the IDs share them.

    Each ID gets the shortest prefix (at least 8 characters, at most the whole ID)
    that no other ID starts with. In sorted order the longest prefix an ID shares
    with any other is the one it shares with a neighbour, so one sort and one pass
    over neighbours suffice.
    """
    ids = sorted({str(row.get("id") or "") for row in rows})
    shown = {}
    for position, record_id in enumerate(ids):
        shared = 0
        if position > 0:
            shared = _common_prefix_length(ids[position - 1], record_id)
        if position + 1 < len(ids):
            shared = max(shared, _common_prefix_length(ids[position + 1], record_id))
        shown[record_id] = record_id[:min(len(record_id), max(SHORT_ID_LENGTH, shared + 1))]
    return shown


def _split_path(path):
    """(folder with a trailing '/', file name) of a row's path."""
    text = str(path or "")
    if "/" not in text:
        return "", text
    folder, name = text.rsplit("/", 1)
    return folder + "/", name


def _size_text(size):
    return "--" if size is None else format_bytes(size)


def _sizes(row):
    return "{} -> {}".format(_size_text(row.get("source_size")), _size_text(row.get("output_size")))


def _output_differs(source_name, output_name):
    """True when the output name is not the source stem plus '.mp4' (interim name, DVD title)."""
    if not output_name:
        return False
    return output_name != os.path.splitext(source_name)[0] + ".mp4"


def _group_by_folder(rows):
    """[(folder, [rows])]: folders in the order of their first (worst) row, rows in report order.

    Rows are grouped by their real folder (FOLDER_GROUP_KEY), so two real folders
    whose redacted forms coincide stay two groups with the same printed folder.
    Rows without the key are grouped by the folder of their path.
    """
    groups = {}
    for row in rows:
        folder, _name = _split_path(row.get("path"))
        key = (row[FOLDER_GROUP_KEY],) if FOLDER_GROUP_KEY in row else folder
        groups.setdefault(key, (folder, []))[1].append(row)
    return list(groups.values())


def _one_line(value):
    """A text-report field on one line: carriage returns and newlines become spaces."""
    return ("" if value is None else str(value)).replace("\r", " ").replace("\n", " ")


def _header_lines(header):
    lines = ["report ID: {}".format(header.get("report_id")),
             "generated at: {}".format(header.get("generated_at")),
             "tool version: {}".format(header.get("tool_version")),
             "invocation: {}".format(header.get("command")),
             "redacted: {}{}".format("yes" if header.get("redacted") else "no",
                                     ", dates to the year" if header.get("redact_dates") else ""),
             "records: {}".format(header.get("records"))]
    if header.get("unreadable_records"):
        lines.append("unreadable records skipped: {}".format(header.get("unreadable_records")))
    if header.get("from_report"):
        lines.append("reissue of report: {}".format(header.get("from_report")))
    return lines


def render_text(header, rows, shown=None):
    """Plain header lines, then one block per row grouped by folder, columns aligned over the report.

    shown: {full ID: shown ID} from short_ids(); computed over rows when not given.
    Every field is kept on its line (carriage returns and newlines become spaces);
    only log text keeps its own lines. A quarantined or failed row gets a 'reason:'
    line under its name line, before 'changed since' and any logs.
    """
    lines = [TEXT_TITLE, "", _one_line(header.get("hint", "")), ""]
    lines.extend(_one_line(line) for line in _header_lines(header))
    if shown is None:
        shown = short_ids(rows)
    table = []
    for row in rows:
        table.append(tuple(_one_line(cell) for cell in (
            _score_text(row.get("score")), row.get("band") or "--", row.get("status") or "--",
            row.get("action") or "--", ", ".join(row.get("flags") or ()) or "--",
            shown.get(str(row.get("id") or ""), ""))))
    widths = [max([len(cells[column]) for cells in table] or [0]) for column in range(6)]
    cells_of = {id(row): cells for row, cells in zip(rows, table)}
    indent = " " * (2 + widths[0] + 2)
    if not rows:
        lines.extend(["", "No records."])
    for folder, members in _group_by_folder(rows):
        lines.extend(["", _one_line(folder or "./")])
        for row in members:
            cells = cells_of[id(row)]
            first = "  " + cells[0].rjust(widths[0]) + "  " + "  ".join(
                cells[column].ljust(widths[column]) for column in range(1, 5)) + "  " + cells[5]
            lines.append(first)
            _folder, name = _split_path(row.get("path"))
            second = indent + _one_line(name)
            if row.get("output_name"):
                second += "  ->  " + _one_line(row["output_name"])
            lines.append(second + "   (" + _sizes(row) + ")")
            if row.get("reason"):
                lines.append(indent + "reason: " + _one_line(row["reason"]))
            if row.get("changed_since"):
                lines.append(indent + "changed since: " + _one_line(row["changed_since"]))
            for entry in row.get("logs") or ():
                lines.append(indent + _one_line("{} {}:".format(entry["stage"], entry["kind"])))
                for log_line in entry["text"].rstrip("\n").split("\n"):
                    lines.append(indent + "  " + log_line)
    if header.get("with_logs") and not any(row.get("logs") for row in rows):
        lines.extend(["", "No stderr or run log is stored in these records."])
    return "\n".join(lines) + "\n"


def render_markdown(header, rows, shown=None):
    """Hint line, header list, machine-readable header comment, table, reasons, folders, then logs if any.

    shown: {full ID: shown ID} from short_ids(); computed over rows when not given.
    """
    lines = ["# vidfunnel report", "", "> " + header.get("hint", ""), ""]
    for line in _header_lines(header):
        if line.startswith("invocation: "):
            line = "invocation: " + _cell(header.get("command"))
        lines.append("- " + line)
    # JSON never puts '>' outside a string, so escaping it keeps '-->' out of the comment.
    machine = json.dumps(header, sort_keys=True, ensure_ascii=False).replace(">", "\\u003e")
    lines.extend(["", HEADER_COMMENT_START + machine + HEADER_COMMENT_END, ""])
    lines.append("| " + " | ".join(MD_COLUMNS) + " |")
    lines.append("|" + "|".join("---" for _column in MD_COLUMNS) + "|")
    if shown is None:
        shown = short_ids(rows)
    for row in rows:
        status = row.get("status") or "--"
        if row.get("changed_since"):
            status = "{} (changed-since: {})".format(status, row["changed_since"])
        _folder, name = _split_path(row.get("path"))
        source = name
        if _output_differs(name, row.get("output_name")):
            source = "{} -> {}".format(name, row["output_name"])
        cells = (shown.get(str(row.get("id") or "")), _score_text(row.get("score")), row.get("band") or "--",
                 status, row.get("action") or "--", ", ".join(row.get("flags") or ()) or "--", source,
                 _sizes(row))
        lines.append("| " + " | ".join(_cell(cell) for cell in cells) + " |")
    explained = [row for row in rows if row.get("reason")]
    if explained:
        lines.extend(["", REASONS_HEADING, ""])
        for row in explained:
            lines.append("- {}: {}".format(shown.get(str(row.get("id") or "")), _one_line(row["reason"])))
    if rows:
        lines.extend(["", FOLDERS_HEADING, ""])
        for folder, members in _group_by_folder(rows):
            lines.append("- {}: {}".format(
                (folder or "./").replace("\r", " ").replace("\n", " "),
                ", ".join(shown.get(str(row.get("id") or "")) for row in members)))
    logged = [row for row in rows if row.get("logs")]
    if header.get("with_logs"):
        lines.extend(["", "## Logs"])
        if not logged:
            lines.extend(["", "No stderr or run log is stored in these records."])
        for row in logged:
            lines.extend(["", "### {}".format(shown.get(str(row.get("id") or "")))])
            for entry in row["logs"]:
                fence = _fence(entry["text"])
                lines.extend(["", "{} {}:".format(entry["stage"], entry["kind"]), "",
                              fence, entry["text"].rstrip("\n"), fence])
    return "\n".join(lines) + "\n"


def render_json(header, rows, shown=None):
    rows = [{key: value for key, value in row.items() if key != FOLDER_GROUP_KEY} for row in rows]
    return json.dumps({"header": header, "rows": rows}, indent=2, sort_keys=True,
                      ensure_ascii=False) + "\n"


def render_csv(header, rows, shown=None):
    """RFC 4180: one column name row, then one row per record. Flags joined with ';'."""
    fields = list(ROW_FIELDS)
    if header.get("with_logs"):
        fields.append("logs")
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(fields)
    for row in rows:
        values = []
        for field in fields:
            value = row.get(field)
            if field == "flags":
                value = ";".join(value or ())
            elif field == "logs":
                value = "\n".join("[{} {}]\n{}".format(entry["stage"], entry["kind"], entry["text"])
                                  for entry in value or ())
            values.append("" if value is None else value)
        writer.writerow(values)
    return buffer.getvalue()


RENDERERS = {"text": render_text, "md": render_markdown, "json": render_json, "csv": render_csv}


# ---------------------------------------------------------------- reading a report back


def _parse_score(text):
    text = text.strip()
    if text in ("", "--", "None", "null"):
        return None
    try:
        return int(text)
    except ValueError:
        return float(text)


def parse_report_header(text):
    """Read a Markdown or JSON report back.

    Returns {'format': 'md'|'json', 'header': dict, 'invocation': list,
    'rows': {id: {'status': ..., 'score': ...}}, 'folders': {id: folder}}. The
    row keys are the IDs as the report shows them: full IDs in JSON and in
    Markdown written before 20261108, prefixes in newer Markdown (see
    resolve_stored_rows). 'folders' comes from the Markdown Folders section
    (empty for JSON and the old layout). Raises ReportUsageError for a text
    report and ReportError for CSV or anything else without a report header.
    """
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except ValueError as error:
            raise ReportError("the report is not valid JSON: {}".format(error))
        if not isinstance(data, dict) or not isinstance(data.get("header"), dict):
            raise ReportError("the JSON report has no 'header' object")
        rows = {}
        for row in data.get("rows") or ():
            if isinstance(row, dict) and row.get("id"):
                rows[row["id"]] = {"status": row.get("status"), "score": row.get("score")}
        header = data["header"]
        return {"format": "json", "header": header, "invocation": list(header.get("invocation") or ()),
                "rows": rows, "folders": {}}
    header = None
    rows = {}
    folders = {}
    in_table = False
    in_folders = False
    columns = 0
    for line in text.splitlines():
        if line.startswith(HEADER_COMMENT_START) and line.endswith(HEADER_COMMENT_END):
            payload = line[len(HEADER_COMMENT_START):len(line) - len(HEADER_COMMENT_END)]
            try:
                header = json.loads(payload)
            except ValueError as error:
                raise ReportError("the report header comment is not valid JSON: {}".format(error))
            continue
        if line.strip() == "## Logs":
            # Everything below is log text, which may hold any line at all.
            break
        if line.startswith("| ID |") and not columns:
            names = tuple(cell.strip() for cell in line.strip().split("|")[1:-1])
            if names not in (MD_COLUMNS, OLD_MD_COLUMNS):
                raise ReportError("the report table has unknown columns: {}".format(", ".join(names)))
            columns = len(names)
            in_table = True
            continue
        if line.startswith("#"):
            in_folders = line.strip() == FOLDERS_HEADING
            continue
        if in_folders and line.startswith("- "):
            folder, separator, listed = line[2:].rpartition(": ")
            if not separator:
                raise ReportError("a line of the Folders section has no ': ': '{}'".format(line))
            for shown_id in listed.split(","):
                if shown_id.strip():
                    folders[shown_id.strip()] = folder
            continue
        if in_table:
            if not line.startswith("|"):
                in_table = False
                continue
            if line.startswith("|---"):
                continue
            cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
            if len(cells) < columns:
                raise ReportError("a report table row has {} cells, expected {}".format(
                    len(cells), columns))
            status = cells[3].split(" ", 1)[0]
            rows[cells[0]] = {"status": None if status == "--" else status,
                              "score": _parse_score(cells[1])}
    if not isinstance(header, dict):
        # A text report saved with its stderr ('> file 2>&1') starts with progress lines,
        # one per PATH and per unreadable record, so the title line is looked for on
        # every line; no Markdown, JSON or CSV report has a line that is only the title.
        if any(line.strip() == TEXT_TITLE for line in text.splitlines()):
            raise ReportUsageError("this is a text report, which is for reading and cannot be read "
                                   "back; give --from a Markdown or JSON report (--format md or json)")
        raise ReportError("no vidfunnel report header found (only Markdown and JSON reports "
                          "can be read back; CSV carries no header)")
    return {"format": "md", "header": header, "invocation": list(header.get("invocation") or ()),
            "rows": rows, "folders": folders}


def resolve_stored_rows(stored_rows, items):
    """Map the IDs of a stored report (full IDs or prefixes) to the records found now.

    Returns ({full ID: stored entry}, [shown IDs no record matches]). Matching is
    the rule of 'vidfunnel find': case-insensitive, the ID equals or starts with
    the shown one. A shown ID that matches more than one record raises ReportError
    naming two of them.
    """
    ids = sorted({(str(record.get("id")).lower(), str(record.get("id")))
                  for _folder, record in items if record.get("id")})
    keys = [lowered for lowered, _record_id in ids]
    resolved = {}
    missing = []
    for shown_id, entry in stored_rows.items():
        prefix = str(shown_id).strip().lower()
        matches = []
        position = bisect.bisect_left(keys, prefix) if prefix else len(keys)
        while position < len(keys) and keys[position].startswith(prefix):
            matches.append(ids[position][1])
            position += 1
        if len(matches) > 1:
            raise ReportError("the ID '{}' of the earlier report matches more than one record: '{}' and "
                              "'{}'{}; give PATH arguments that hold only one of them".format(
                                  shown_id, matches[0], matches[1],
                                  " and {} more".format(len(matches) - 2) if len(matches) > 2 else ""))
        if matches:
            resolved[matches[0]] = entry
        else:
            missing.append(shown_id)
    return resolved, missing


# ---------------------------------------------------------------- command line


def add_arguments(parser):
    parser.add_argument("paths", nargs="*", metavar="PATH",
                        help="folders (walked for .vidfunnel records) or source files")
    parser.add_argument("--format", choices=FORMATS, default=None,
                        help="output format: text (the default, for reading), md, json or csv")
    parser.add_argument("--min-score", type=int, default=None, metavar="N",
                        help="only rows with a score of at least N (drops unscored rows)")
    parser.add_argument("--max-score", type=int, default=None, metavar="N",
                        help="only rows with a score of at most N (drops unscored rows)")
    parser.add_argument("--status", action="append", choices=STATUSES, default=None, metavar="S",
                        help="only rows with this status; repeatable")
    parser.add_argument("--reveal", action="store_true", help="show real names and paths")
    parser.add_argument("--redact-dates", action="store_true",
                        help="truncate creation times and mtimes to the year")
    parser.add_argument("--with-logs", action="store_true",
                        help="add the stored ffmpeg stderr and run log of each row")
    parser.add_argument("--from", dest="from_report", default=None, metavar="REPORT",
                        help="re-run the invocation recorded in an earlier Markdown or JSON report")
    parser.add_argument("--index", action="store_true",
                        help="read the central index (every tree ever indexed) instead of a PATH")
    parser.add_argument("--no-index", action="store_true",
                        help="never use the central index, always read the records")
    parser.add_argument("--db", default=None, metavar="PATH",
                        help="central index database to use")


def _stored_args(invocation):
    """Parse a stored invocation ('vidfunnel', 'report', ...) with this module's arguments."""
    if len(invocation) < 2 or invocation[0] != "vidfunnel" or invocation[1] != "report":
        raise ReportError("the stored invocation is not a 'vidfunnel report' command: {}".format(
            " ".join(shlex.quote(str(part)) for part in invocation)))
    parser = argparse.ArgumentParser(prog="vidfunnel report", add_help=False)
    add_arguments(parser)
    try:
        return parser.parse_args([str(part) for part in invocation[2:]])
    except SystemExit:
        raise ReportError("the stored invocation cannot be parsed: {}".format(
            " ".join(shlex.quote(str(part)) for part in invocation)))


def _index_usable(args):
    """True when produce() may read the central index: no --no-index and the database file exists."""
    if getattr(args, "no_index", False):
        return False
    location = getattr(args, "db", None)
    if not location:
        try:
            from vidfunnel import index as index_module
        except ImportError:
            return False
        location = index_module.default_db_path()
    return os.path.isfile(str(location))


def _merge(current, stored, folders=None):
    """The stored invocation with whatever the current command line adds or overrides.

    folders: the folders of a Markdown report's Folders section. When the stored
    report was revealed, its invocation names no PATH (it read the index) and the
    current command line gives none, the index is read again when it is usable
    (--no-index not given, the database file exists); otherwise those folders
    become the PATHs and only the stored rows are kept ('only_stored_rows'). A
    listed folder that no longer exists is skipped with a line on stderr.
    """
    merged = argparse.Namespace(**vars(stored))
    for attribute in ("format", "min_score", "max_score", "status", "db"):
        value = getattr(current, attribute, None)
        if value is not None:
            setattr(merged, attribute, value)
    for attribute in ("reveal", "redact_dates", "with_logs", "no_index"):
        if getattr(current, attribute, False):
            setattr(merged, attribute, True)
    stored_paths = [path for path in stored.paths or () if not str(path).startswith(ROOT_MARK)]
    redacted_paths = len(stored_paths) != len(stored.paths or ())
    merged.only_stored_rows = False
    if getattr(current, "paths", None):
        merged.paths = list(current.paths)
    elif redacted_paths:
        merged.paths = []
    else:
        merged.paths = stored_paths
    real_folders = []
    for folder in (folders or {}).values():
        if folder and not folder.startswith(ROOT_MARK) and folder not in real_folders:
            real_folders.append(folder)
    if (not merged.paths and not redacted_paths and stored.reveal and real_folders
            and not _index_usable(merged)):
        merged.paths = []
        for folder in real_folders:
            if os.path.isdir(folder):
                merged.paths.append(folder)
            else:
                _stderr("report: skipping folder '{}' of the earlier report: it no longer exists".format(
                    folder))
        merged.only_stored_rows = True
    merged.from_report = None
    for attribute in ("quiet", "verbose"):
        setattr(merged, attribute, getattr(current, attribute, False))
    return merged, redacted_paths


def produce(args, stored=None, from_report_id=None):
    """Collect, filter, redact, sort and render; returns (text, header)."""
    paths = list(getattr(args, "paths", None) or ())
    wants_index = bool(getattr(args, "db", None)) or bool(getattr(args, "index", False))
    if not paths and not wants_index and stored is None and not getattr(args, "no_index", False):
        # Like plan, convert, verify and index: no PATH means the current directory. The
        # central index is read only when asked for (--index, --db) or when a report is
        # re-run from a stored invocation without paths.
        here = os.getcwd()
        _stderr("vidfunnel report: no PATH given; reading the current directory '{}'".format(here), args)
        paths = [here]
    use_index = not getattr(args, "no_index", False) and (not paths or wants_index)
    if not paths and not use_index:
        raise ReportError("no PATH given and --no-index set: nothing to read")
    errors = []
    for path in paths:
        _stderr("report: reading records under '{}'".format(path), args)
    items = collect(paths, use_index=use_index, db_path=getattr(args, "db", None), errors=errors)
    # Shown IDs are made unique over everything collected, before any filter, which is
    # also what --from matches them against (resolve_stored_rows).
    shown = short_ids([{"id": record.get("id")} for _folder, record in items])
    collected = items
    stored_rows = {}
    if stored is not None:
        stored_rows, unmatched = resolve_stored_rows(stored["rows"], items)
        for shown_id in unmatched:
            _stderr("report: record {} from the earlier report was not found now".format(shown_id), args)
        if not paths or getattr(args, "only_stored_rows", False):
            items = [(folder, record) for folder, record in items if record.get("id") in stored_rows]
    items = filter_items(items, getattr(args, "min_score", None), getattr(args, "max_score", None),
                         getattr(args, "status", None))
    redactor = None
    if not getattr(args, "reveal", False):
        roots = roots_for(paths)
        if not roots and items:
            roots = [os.path.commonpath([str(Path(folder).absolute()) for folder, _record in items])]
        redactor = Redactor(roots=roots, redact_dates=getattr(args, "redact_dates", False))
        # Every collected record is learned, filtered out or not: a reason or log of a row
        # may name another record's file (an output collision names the other source).
        for _folder, record in collected:
            redactor.learn(record)
    rows = rows_from_records(items, redactor=redactor, with_logs=getattr(args, "with_logs", False), args=args)
    if stored is not None:
        missing = mark_changed(rows, stored_rows)
        for record_id in missing:
            _stderr("report: record {} from the earlier report is not in the rows now".format(record_id),
                    args)
    rows = sort_rows(rows)
    header = build_header(args, len(rows), redactor=redactor, from_report=from_report_id,
                          unreadable=len(errors))
    form = getattr(args, "format", None) or DEFAULT_FORMAT
    text = RENDERERS[form](header, rows, shown=shown)
    if redactor is not None:
        text = redactor.text(text)
    return text, header


def run(args):
    from vidfunnel.cli import EXIT_ERROR, EXIT_OK, EXIT_USAGE

    stored = None
    from_report_id = None
    try:
        if getattr(args, "from_report", None):
            location = args.from_report
            try:
                with open(location, "r", encoding="utf-8") as handle:
                    stored = parse_report_header(handle.read())
            except OSError as error:
                say("report: cannot read '{}': {}".format(location, error), stream=sys.stderr)
                return EXIT_ERROR
            except ReportUsageError as error:
                say("report: '{}': {}".format(location, error), stream=sys.stderr)
                return EXIT_USAGE
            from_report_id = stored["header"].get("report_id")
            stored_args = _stored_args(stored["invocation"])
            args, redacted_paths = _merge(args, stored_args, stored.get("folders"))
            if redacted_paths and not args.paths and getattr(args, "no_index", False):
                say("report: '{}' records its paths redacted as '<root>'; give the PATH arguments "
                    "again, or drop --no-index to find the rows in the index".format(location),
                    stream=sys.stderr)
                return EXIT_USAGE
        elif not getattr(args, "paths", None) and getattr(args, "no_index", False):
            say("report: give at least one PATH, or use the index (no --no-index)", stream=sys.stderr)
            return EXIT_USAGE
        text, header = produce(args, stored=stored, from_report_id=from_report_id)
    except ReportError as error:
        say("report: {}".format(error), stream=sys.stderr)
        return EXIT_ERROR
    sys.stdout.write(text)
    sys.stdout.flush()
    if (getattr(args, "format", None) or DEFAULT_FORMAT) == "csv":
        _stderr("report {}: {}".format(header["report_id"], header["hint"]), args)
    _stderr("report: {} rows, {}".format(header["records"],
                                         "redacted" if header["redacted"] else "names shown"), args)
    return EXIT_OK

# End of File #
