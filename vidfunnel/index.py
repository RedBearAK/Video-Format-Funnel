"""Optional SQLite index built from the per-folder records (HANDOFF.md section 6.7 point 3).

The index is a cache, never the authority: every value in it comes from a
record file, and deleting the database loses nothing. 'vidfunnel index PATH...'
walks the trees and upserts one row per record; 'vidfunnel find ID' resolves a
record ID (or a unique prefix) through the index when it holds the ID, and
otherwise by walking the given roots (default: the current directory).
"""

VERSION = "20261107"

import json
import os
import sqlite3
import sys
from pathlib import Path

from vidfunnel.progress import say
from vidfunnel.record import RECORD_DIR, RecordError, iter_records, now_iso, record_path

DEFAULT_DB = Path("~/.local/state/vidfunnel/index.sqlite")
SCHEMA_VERSION = "1"

COLUMNS = (
    "id", "folder", "source_name", "record_path", "status", "score", "action", "flags",
    "source_size", "output_name", "output_size", "tool_version", "status_at", "indexed_at", "json",
)

_CREATE_RECORDS = """
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY,
    folder TEXT,
    source_name TEXT,
    record_path TEXT,
    status TEXT,
    score INTEGER,
    action TEXT,
    flags TEXT,
    source_size INTEGER,
    output_name TEXT,
    output_size INTEGER,
    tool_version TEXT,
    status_at TEXT,
    indexed_at TEXT,
    json TEXT
)
"""
_CREATE_META = "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"


class IndexMissing(FileNotFoundError):
    """open_index(create=False) was asked for a database file that does not exist."""


def _warn(message):
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


# ---------------------------------------------------------------- database


def default_db_path(home=None):
    """The default database path, '~' expanded (against home when given)."""
    if home is None:
        return DEFAULT_DB.expanduser()
    return Path(home) / Path(*DEFAULT_DB.parts[1:])


def _create_schema(conn):
    conn.execute(_CREATE_RECORDS)
    conn.execute(_CREATE_META)
    conn.execute("CREATE INDEX IF NOT EXISTS records_folder ON records (folder)")
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))


def _meta_value(conn, key):
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def open_index(db_path, create=True):
    """Open (and with create=True create) the index; returns a sqlite3.Connection.

    With create=False a missing file raises IndexMissing instead of creating one.
    A database written by another schema version is dropped and recreated,
    with a warning: it is a cache and every row comes back from the records.
    sqlite3.DatabaseError propagates when the file is not a usable database.
    """
    db_path = Path(db_path).expanduser()
    if create:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
    else:
        if not db_path.is_file():
            raise IndexMissing("no index at '{}'".format(db_path))
        conn = sqlite3.connect("file:{}?mode=rw".format(db_path.as_posix()), uri=True)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        found_version = _meta_value(conn, "schema_version")
        if found_version is not None and found_version != SCHEMA_VERSION:
            _warn("warning: index '{}' has schema version {}, expected {}; rebuilding it".format(
                db_path, found_version, SCHEMA_VERSION))
            rebuild(conn)
        else:
            with conn:
                _create_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def rebuild(conn):
    """Drop both tables and create them empty."""
    with conn:
        conn.execute("DROP TABLE IF EXISTS records")
        conn.execute("DROP TABLE IF EXISTS meta")
        _create_schema(conn)


# ---------------------------------------------------------------- rows


def _int_or_none(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def upsert_record(conn, folder, record):
    """Insert or replace the row of one record found in folder. Does not commit.

    ValueError when the record has no string 'id'.
    """
    record_id = record.get("id")
    if not isinstance(record_id, str) or not record_id:
        raise ValueError("record in '{}' has no id".format(folder))
    folder_text = os.path.abspath(str(folder))
    source = record.get("source") or {}
    output = record.get("output") or {}
    source_name = source.get("name")
    location = record_path(Path(folder_text) / source_name) if source_name else None
    flags = record.get("flags") or []
    values = (
        record_id,
        folder_text,
        source_name,
        str(location) if location is not None else None,
        record.get("status"),
        _int_or_none(record.get("score")),
        record.get("action"),
        json.dumps(flags, ensure_ascii=False),
        _int_or_none(source.get("size")),
        output.get("name"),
        _int_or_none(output.get("size")),
        record.get("tool_version"),
        record.get("status_at"),
        now_iso(),
        json.dumps(record, sort_keys=True, ensure_ascii=False),
    )
    conn.execute(
        "INSERT OR REPLACE INTO records ({}) VALUES ({})".format(", ".join(COLUMNS), ", ".join("?" * len(COLUMNS))),
        values,
    )


def _is_under(folder, root):
    """True when folder is root or lies below it (plain path-string comparison)."""
    if folder == root:
        return True
    prefix = root if root.endswith(os.sep) else root + os.sep
    return folder.startswith(prefix)


def _row_from_record(folder, record, record_file=None, indexed_at=None, from_index=False):
    """The row dict that find() and query() return, built from one record."""
    folder_text = os.path.abspath(str(folder))
    source = record.get("source") or {}
    output = record.get("output") or {}
    source_name = source.get("name")
    output_name = output.get("name")
    if record_file is None and source_name:
        record_file = str(record_path(Path(folder_text) / source_name))
    return {
        "id": record.get("id"),
        "folder": folder_text,
        "source_name": source_name,
        "source_path": os.path.join(folder_text, source_name) if source_name else None,
        "record_path": record_file,
        "status": record.get("status"),
        "score": _int_or_none(record.get("score")),
        "action": record.get("action"),
        "flags": list(record.get("flags") or []),
        "source_size": _int_or_none(source.get("size")),
        "output_name": output_name,
        "output_path": os.path.join(folder_text, output_name) if output_name else None,
        "output_size": _int_or_none(output.get("size")),
        "tool_version": record.get("tool_version"),
        "status_at": record.get("status_at"),
        "indexed_at": indexed_at,
        "record": record,
        "from_index": from_index,
    }


def _row_from_db(db_row):
    values = dict(zip(COLUMNS, db_row))
    record = json.loads(values["json"])
    return _row_from_record(values["folder"], record, record_file=values["record_path"],
                            indexed_at=values["indexed_at"], from_index=True)


def _select_rows(conn, where="", parameters=()):
    sql = "SELECT {} FROM records{} ORDER BY folder, source_name, id".format(
        ", ".join(COLUMNS), (" WHERE " + where) if where else "")
    return [_row_from_db(db_row) for db_row in conn.execute(sql, parameters)]


# ---------------------------------------------------------------- indexing


def index_tree(conn, root, progress_say=None):
    """Upsert every record under root and drop rows under root that the walk no longer finds.

    Returns {'folders': n, 'records': n, 'removed': n, 'errors': n}. 'removed'
    counts rows whose folder is under root and whose ID no record under root
    holds any more (record file deleted, or replaced by a re-plan under a new
    ID). Unreadable records are reported through progress_say, counted in
    'errors', and their existing rows are kept.
    """
    if progress_say is None:
        progress_say = say
    root_text = os.path.abspath(str(root))
    if not os.path.isdir(root_text):
        raise FileNotFoundError("'{}' is not a directory".format(root_text))
    progress_say("[index] reading records under '{}'".format(root_text))
    unreadable = set()

    def on_error(path, error):
        unreadable.add(os.path.abspath(str(path)))
        progress_say("[index] skipped unreadable record '{}': {}".format(path, error))

    without_id = 0
    seen_ids = set()
    folders = set()
    count = 0
    with conn:
        for folder, record in iter_records(root_text, on_error=on_error):
            record_id = record.get("id")
            if not isinstance(record_id, str) or not record_id:
                without_id += 1
                progress_say("[index] skipped record without an id in '{}'".format(Path(folder) / RECORD_DIR))
                continue
            upsert_record(conn, folder, record)
            seen_ids.add(record_id)
            folders.add(os.path.abspath(str(folder)))
            count += 1
        stale = []
        for record_id, folder_text, record_file in conn.execute("SELECT id, folder, record_path FROM records"):
            if record_id in seen_ids or not _is_under(folder_text, root_text):
                continue
            if record_file in unreadable:
                continue
            stale.append(record_id)
        conn.executemany("DELETE FROM records WHERE id = ?", [(record_id,) for record_id in stale])
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_indexed', ?)", (now_iso(),))
    return {"folders": len(folders), "records": count, "removed": len(stale), "errors": len(unreadable) + without_id}


# ---------------------------------------------------------------- lookups


def _normalise_prefix(id_prefix):
    text = str(id_prefix).strip().lower()
    if not text:
        raise ValueError("empty record ID")
    return text


def _walk_matches(prefix, roots, progress_say):
    matches = []
    seen_files = set()
    for root in roots:
        root_text = os.path.abspath(str(root))
        progress_say("[find] searching records under '{}'".format(root_text))

        def on_error(path, error):
            progress_say("[find] skipped unreadable record '{}': {}".format(path, error))

        for folder, record in iter_records(root_text, on_error=on_error):
            record_id = record.get("id")
            if not isinstance(record_id, str) or not record_id.lower().startswith(prefix):
                continue
            row = _row_from_record(folder, record)
            key = row["record_path"] or (row["folder"], record_id)
            if key in seen_files:
                continue
            seen_files.add(key)
            matches.append(row)
    return matches


def find(conn_or_none, id_prefix, roots=(), progress_say=None):
    """Rows whose ID equals id_prefix or starts with it (case-insensitive).

    Looks in the index first; when conn_or_none is None or the index has no
    match, walks roots (default: the current directory) with iter_records.
    Several rows mean the prefix is ambiguous; the caller says so.
    ValueError on an empty ID.
    """
    if progress_say is None:
        progress_say = say
    prefix = _normalise_prefix(id_prefix)
    if conn_or_none is not None:
        rows = _select_rows(conn_or_none, "substr(lower(id), 1, ?) = ?", (len(prefix), prefix))
        if rows:
            return rows
    if not roots:
        roots = (os.getcwd(),)
    return _walk_matches(prefix, roots, progress_say)


def query(conn, status=None, min_score=None, max_score=None, folder_prefix=None):
    """Rows from the index filtered by status, score band (inclusive) and folder.

    status: one status string or a sequence of them. A score bound excludes
    rows without a score. folder_prefix matches that folder and everything
    below it.
    """
    clauses = []
    parameters = []
    if status is not None:
        statuses = [status] if isinstance(status, str) else list(status)
        clauses.append("status IN ({})".format(", ".join("?" * len(statuses))))
        parameters.extend(statuses)
    if min_score is not None:
        clauses.append("score IS NOT NULL AND score >= ?")
        parameters.append(int(min_score))
    if max_score is not None:
        clauses.append("score IS NOT NULL AND score <= ?")
        parameters.append(int(max_score))
    rows = _select_rows(conn, " AND ".join(clauses), tuple(parameters))
    if folder_prefix is not None:
        prefix_text = os.path.abspath(str(folder_prefix))
        rows = [row for row in rows if _is_under(row["folder"], prefix_text)]
    return rows


# ---------------------------------------------------------------- subcommands


def _db_path_from(args):
    db = getattr(args, "db", None)
    return Path(db).expanduser() if db else default_db_path()


def add_arguments(parser):
    """Arguments of 'vidfunnel index'."""
    parser.add_argument("paths", nargs="*", metavar="PATH",
                        help="trees whose records are indexed (default: the current directory)")
    parser.add_argument("--rebuild", action="store_true", help="drop and recreate the index before indexing")
    parser.add_argument("--db", metavar="PATH", default=None,
                        help="index database (default: {})".format(DEFAULT_DB))


def run(args):
    """'vidfunnel index': walk each PATH and load its records into the index."""
    from vidfunnel.cli import EXIT_ERROR, EXIT_OK, EXIT_USAGE, paths_or_current_directory

    paths_or_current_directory(args, "index")
    missing = [path for path in args.paths if not os.path.isdir(str(path))]
    for path in missing:
        _warn("vidfunnel index: '{}' is not a directory".format(path))
    if missing:
        return EXIT_USAGE
    db_path = _db_path_from(args)
    try:
        conn = open_index(db_path, create=True)
    except (OSError, sqlite3.DatabaseError) as error:
        _warn("vidfunnel index: cannot open index '{}': {}".format(db_path, error))
        return EXIT_ERROR
    progress_say = (lambda message: None) if getattr(args, "quiet", False) else say
    totals = {"folders": 0, "records": 0, "removed": 0, "errors": 0}
    try:
        if args.rebuild:
            progress_say("[index] rebuilding '{}'".format(db_path))
            rebuild(conn)
        for path in args.paths:
            try:
                counts = index_tree(conn, path, progress_say=progress_say)
            except (OSError, RecordError, sqlite3.DatabaseError) as error:
                _warn("vidfunnel index: failed on '{}': {}".format(path, error))
                return EXIT_ERROR
            for key in totals:
                totals[key] += counts[key]
    finally:
        conn.close()
    print("index: {} records from {} folders, {} removed, {} unreadable; database '{}'".format(
        totals["records"], totals["folders"], totals["removed"], totals["errors"], db_path))
    return EXIT_ERROR if totals["errors"] else EXIT_OK


def add_find_arguments(parser):
    """Arguments of 'vidfunnel find'."""
    parser.add_argument("id", metavar="ID", help="record ID or any unique prefix of one")
    parser.add_argument("--db", metavar="PATH", default=None,
                        help="index database (default: {})".format(DEFAULT_DB))
    parser.add_argument("--root", metavar="PATH", action="append", default=None,
                        help="tree to search when the index lacks the ID (repeatable; default: current directory)")


def format_row(row):
    """One plain-text block of 'key: value' lines for a find result, paths unredacted."""
    score = row["score"]
    lines = [
        "id: {}".format(row["id"]),
        "status: {}".format(row["status"] if row["status"] is not None else "(none)"),
        "score: {}".format(score if score is not None else "(none)"),
        "action: {}".format(row["action"] if row["action"] is not None else "(none)"),
        "source: {}".format(row["source_path"] if row["source_path"] else "(unknown)"),
        "output: {}".format(row["output_path"] if row["output_path"] else "(none)"),
        "record: {}".format(row["record_path"] if row["record_path"] else "(unknown)"),
    ]
    if row["record_path"] and not os.path.isfile(row["record_path"]):
        lines.append("note: record file not found; the index is stale, re-run 'vidfunnel index'")
    return "\n".join(lines)


def run_find(args):
    """'vidfunnel find ID': print the matching record(s); EXIT_ERROR when none or ambiguous."""
    from vidfunnel.cli import EXIT_ERROR, EXIT_OK, EXIT_USAGE

    try:
        prefix = _normalise_prefix(args.id)
    except ValueError:
        _warn("vidfunnel find: the record ID is empty")
        return EXIT_USAGE
    db_path = _db_path_from(args)
    conn = None
    if db_path.is_file():
        try:
            conn = open_index(db_path, create=False)
        except (OSError, sqlite3.DatabaseError) as error:
            _warn("vidfunnel find: cannot open index '{}': {}; searching the records instead".format(db_path, error))
            conn = None
    elif getattr(args, "db", None):
        _warn("vidfunnel find: no index at '{}'; searching the records instead".format(db_path))
    if getattr(args, "quiet", False):
        progress_say = lambda message: None
    else:
        progress_say = lambda message: say(message, stream=sys.stderr)
    roots = tuple(args.root) if getattr(args, "root", None) else ()
    try:
        rows = find(conn, prefix, roots=roots, progress_say=progress_say)
    except (OSError, sqlite3.DatabaseError) as error:
        _warn("vidfunnel find: {}".format(error))
        return EXIT_ERROR
    finally:
        if conn is not None:
            conn.close()
    if not rows:
        _warn("vidfunnel find: no record matches '{}'".format(prefix))
        return EXIT_ERROR
    print("\n\n".join(format_row(row) for row in rows))
    if len(rows) > 1:
        _warn("vidfunnel find: '{}' is ambiguous: it matches {} records; give more of the ID".format(
            prefix, len(rows)))
        return EXIT_ERROR
    return EXIT_OK

# End of File #
