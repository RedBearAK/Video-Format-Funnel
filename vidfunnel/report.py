"""The 'report' subcommand: a sorted table built from the records (HANDOFF.md sections 6.6 and 6.8).

A report only reads records; it never probes, encodes or writes anything but its
output. Names, paths, metadata and log text are redacted by default through
vidfunnel.redact.Redactor; --reveal turns that off. Record IDs are always shown,
so any row can be resolved locally with 'vidfunnel find ID'.

Sort order (HANDOFF section 5, "worst at the top"): rows without a score come
first (planned, failed or quarantined records that verify never scored), then
scores ascending, then status in the order of record.STATUSES, then path, then ID.

Every report starts with a header: report ID, generation time, tool version,
the invocation, the redaction state and a hint line. Markdown carries the
header both as a readable list and as one machine-readable HTML comment, and
JSON carries it under "header", so 'report --from FILE' can read the invocation
and each row's status and score back. CSV is plain RFC 4180 data (one column
name row, then one row per record); its header goes to stderr and it cannot be
used with --from.

A redacted report records its paths as '<root>', which is what keeps the
directory structure out of it. '--from' on such a report therefore needs the
PATH arguments again, or the central index (rows are then found by record ID).
"""

VERSION = "20261102"

import argparse
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
from vidfunnel.record import STATUSES, STAGES, RecordError, iter_records, load_record, now_iso
from vidfunnel.redact import ROOT_MARK, Redactor, redact_path

FORMATS = ("md", "json", "csv")
DEFAULT_FORMAT = "md"

HINT_REDACTED = (
    "Names in this report are redacted. To see them, re-run the same command with --reveal, "
    "or vidfunnel report --from <this file> --reveal. To locate one row: vidfunnel find <ID>."
)
HINT_REVEALED = (
    "Names in this report are shown (--reveal); do not share it as it is. "
    "To locate one row: vidfunnel find <ID>."
)

HEADER_COMMENT_START = "<!-- vidfunnel-report-header "
HEADER_COMMENT_END = " -->"

# Row fields, in CSV column order. 'logs' is added only with --with-logs.
ROW_FIELDS = (
    "id", "score", "band", "status", "action", "flags", "quarantine_reason",
    "source_size", "output_size", "ratio", "path", "output_name", "changed_since",
)
LOG_KEYS = ("stderr", "log", "run_log")
MD_COLUMNS = ("ID", "score", "band", "status", "action", "flags", "source -> output", "path")

BAND_OK = 90
BAND_REVIEW = 60


class ReportError(Exception):
    """A report cannot be produced or read back; the message says why."""


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


def band(score):
    """HANDOFF section 5: >= 90 'ok', 60-89 'review', < 60 'hold'; None for no score."""
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        return None
    if score >= BAND_OK:
        return "ok"
    if score >= BAND_REVIEW:
        return "review"
    return "hold"


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


def rows_from_records(items, redactor=None, with_logs=False):
    """One row dict per (folder, record); with a redactor, rows come from redacted copies.

    Every record is passed to redactor.record() before any row is built, so that
    the final redactor.text() pass over each string field knows every real name.
    """
    prepared = []
    for folder, record in items:
        if redactor is not None:
            prepared.append(redactor.record(record))
        else:
            prepared.append(record)

    def clean(value):
        if redactor is not None and isinstance(value, str):
            return redactor.text(value)
        return value

    rows = []
    for record in prepared:
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
            "output_name": clean(output.get("name") or output.get("interim_name")),
            "changed_since": None,
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
        "format": getattr(args, "format", None) or DEFAULT_FORMAT,
        "redacted": redacted,
        "redact_dates": bool(getattr(args, "redact_dates", False)),
        "with_logs": bool(getattr(args, "with_logs", False)),
        "records": records_count,
        "unreadable_records": unreadable,
        "hint": HINT_REDACTED if redacted else HINT_REVEALED,
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


def render_markdown(header, rows):
    """Hint line, header list, machine-readable header comment, table, then logs if any."""
    lines = ["# vidfunnel report", "", "> " + header.get("hint", ""), ""]
    lines.append("- report ID: {}".format(header.get("report_id")))
    lines.append("- generated at: {}".format(header.get("generated_at")))
    lines.append("- tool version: {}".format(header.get("tool_version")))
    lines.append("- invocation: {}".format(_cell(header.get("command"))))
    lines.append("- redacted: {}{}".format("yes" if header.get("redacted") else "no",
                                           ", dates to the year" if header.get("redact_dates") else ""))
    lines.append("- records: {}".format(header.get("records")))
    if header.get("unreadable_records"):
        lines.append("- unreadable records skipped: {}".format(header.get("unreadable_records")))
    if header.get("from_report"):
        lines.append("- reissue of report: {}".format(header.get("from_report")))
    # JSON never puts '>' outside a string, so escaping it keeps '-->' out of the comment.
    machine = json.dumps(header, sort_keys=True, ensure_ascii=False).replace(">", "\\u003e")
    lines.extend(["", HEADER_COMMENT_START + machine + HEADER_COMMENT_END, ""])
    lines.append("| " + " | ".join(MD_COLUMNS) + " |")
    lines.append("|" + "|".join("---" for _column in MD_COLUMNS) + "|")
    for row in rows:
        status = row.get("status") or "--"
        if row.get("changed_since"):
            status = "{} (changed-since: {})".format(status, row["changed_since"])
        sizes = "{} -> {}".format(format_bytes(row.get("source_size")),
                                  format_bytes(row.get("output_size")))
        if row.get("output_name"):
            sizes = "{} ({})".format(sizes, row["output_name"])
        cells = (row.get("id"), _score_text(row.get("score")), row.get("band") or "--", status,
                 row.get("action") or "--", ", ".join(row.get("flags") or ()) or "--", sizes,
                 row.get("path"))
        lines.append("| " + " | ".join(_cell(cell) for cell in cells) + " |")
    logged = [row for row in rows if row.get("logs")]
    if header.get("with_logs"):
        lines.extend(["", "## Logs"])
        if not logged:
            lines.extend(["", "No stderr or run log is stored in these records."])
        for row in logged:
            lines.extend(["", "### {}".format(row.get("id"))])
            for entry in row["logs"]:
                fence = _fence(entry["text"])
                lines.extend(["", "{} {}:".format(entry["stage"], entry["kind"]), "",
                              fence, entry["text"].rstrip("\n"), fence])
    return "\n".join(lines) + "\n"


def render_json(header, rows):
    return json.dumps({"header": header, "rows": rows}, indent=2, sort_keys=True,
                      ensure_ascii=False) + "\n"


def render_csv(header, rows):
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


RENDERERS = {"md": render_markdown, "json": render_json, "csv": render_csv}


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
    'rows': {id: {'status': ..., 'score': ...}}}. Raises ReportError for CSV or
    anything else without a vidfunnel report header.
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
                "rows": rows}
    header = None
    rows = {}
    in_table = False
    for line in text.splitlines():
        if line.startswith(HEADER_COMMENT_START) and line.endswith(HEADER_COMMENT_END):
            payload = line[len(HEADER_COMMENT_START):len(line) - len(HEADER_COMMENT_END)]
            try:
                header = json.loads(payload)
            except ValueError as error:
                raise ReportError("the report header comment is not valid JSON: {}".format(error))
            continue
        if line.startswith("| ID |"):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                in_table = False
                continue
            if line.startswith("|---"):
                continue
            cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
            if len(cells) < len(MD_COLUMNS):
                raise ReportError("a report table row has {} cells, expected {}".format(
                    len(cells), len(MD_COLUMNS)))
            status = cells[3].split(" ", 1)[0]
            rows[cells[0]] = {"status": None if status == "--" else status,
                              "score": _parse_score(cells[1])}
    if not isinstance(header, dict):
        raise ReportError("no vidfunnel report header found (only Markdown and JSON reports "
                          "can be read back; CSV carries no header)")
    return {"format": "md", "header": header, "invocation": list(header.get("invocation") or ()),
            "rows": rows}


# ---------------------------------------------------------------- command line


def add_arguments(parser):
    parser.add_argument("paths", nargs="*", metavar="PATH",
                        help="folders (walked for .vidfunnel records) or source files")
    parser.add_argument("--format", choices=FORMATS, default=None,
                        help="output format (default md)")
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


def _merge(current, stored):
    """The stored invocation with whatever the current command line adds or overrides."""
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
    if getattr(current, "paths", None):
        merged.paths = list(current.paths)
    elif redacted_paths:
        merged.paths = []
    else:
        merged.paths = stored_paths
    merged.from_report = None
    for attribute in ("quiet", "verbose"):
        setattr(merged, attribute, getattr(current, attribute, False))
    return merged, redacted_paths


def produce(args, stored=None, from_report_id=None):
    """Collect, filter, redact, sort and render; returns (text, header)."""
    paths = list(getattr(args, "paths", None) or ())
    use_index = not getattr(args, "no_index", False) and (not paths or bool(getattr(args, "db", None)))
    if not paths and not use_index:
        raise ReportError("no PATH given and --no-index set: nothing to read")
    errors = []
    for path in paths:
        _stderr("report: reading records under '{}'".format(path), args)
    items = collect(paths, use_index=use_index, db_path=getattr(args, "db", None), errors=errors)
    if not paths and stored is not None:
        wanted = set(stored["rows"])
        items = [(folder, record) for folder, record in items if record.get("id") in wanted]
    items = filter_items(items, getattr(args, "min_score", None), getattr(args, "max_score", None),
                         getattr(args, "status", None))
    redactor = None
    if not getattr(args, "reveal", False):
        roots = roots_for(paths)
        if not roots and items:
            roots = [os.path.commonpath([str(Path(folder).absolute()) for folder, _record in items])]
        redactor = Redactor(roots=roots, redact_dates=getattr(args, "redact_dates", False))
    rows = rows_from_records(items, redactor=redactor, with_logs=getattr(args, "with_logs", False))
    if stored is not None:
        missing = mark_changed(rows, stored["rows"])
        for record_id in missing:
            _stderr("report: record {} from the earlier report was not found now".format(record_id), args)
    rows = sort_rows(rows)
    header = build_header(args, len(rows), redactor=redactor, from_report=from_report_id,
                          unreadable=len(errors))
    form = getattr(args, "format", None) or DEFAULT_FORMAT
    text = RENDERERS[form](header, rows)
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
            from_report_id = stored["header"].get("report_id")
            stored_args = _stored_args(stored["invocation"])
            args, redacted_paths = _merge(args, stored_args)
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
