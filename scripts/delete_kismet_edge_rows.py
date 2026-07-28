#!/usr/bin/env python3
"""Delete complete rows from both time edges of Kismet databases in-place.

No backups are created. Rows in tables with a ``ts_sec`` column are deleted
when they fall in the first or last interval. Aggregate device rows are deleted
when their lifetime overlaps either interval; retained rows are never rewritten.
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path


DEFAULT_SECONDS = 120


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path,
                        help=".kismet files or directories (directories are recursive)")
    parser.add_argument("--seconds", type=int, default=DEFAULT_SECONDS,
                        help=f"seconds to delete from each end (default: {DEFAULT_SECONDS})")
    return parser.parse_args()


def quote_identifier(value):
    return '"' + value.replace('"', '""') + '"'


def table_columns(connection, table):
    quoted = quote_identifier(table)
    return {row[1] for row in connection.execute(f"PRAGMA table_info({quoted})")}


def find_databases(paths):
    found = set()
    for path in paths:
        if path.is_dir():
            candidates = path.rglob("*.kismet")
        elif path.is_file() and path.suffix.casefold() == ".kismet":
            candidates = (path,)
        else:
            print(f"warning: not a .kismet file or directory: {path}", file=sys.stderr)
            continue
        found.update(candidate.resolve() for candidate in candidates if candidate.is_file())
    return sorted(found)


def capture_bounds(connection, tables):
    if "packets" in tables and "ts_sec" in table_columns(connection, "packets"):
        row = connection.execute("SELECT min(ts_sec), max(ts_sec) FROM packets").fetchone()
        if row[0] is not None:
            return int(row[0]), int(row[1])

    sources = []
    for table in tables:
        if "ts_sec" in table_columns(connection, table):
            sources.append(f"SELECT ts_sec AS stamp FROM {quote_identifier(table)}")
    if sources:
        row = connection.execute(
            "SELECT min(stamp), max(stamp) FROM (" + " UNION ALL ".join(sources) + ")"
        ).fetchone()
        if row[0] is not None:
            return int(row[0]), int(row[1])

    if "devices" in tables:
        columns = table_columns(connection, "devices")
        if {"first_time", "last_time"}.issubset(columns):
            row = connection.execute("SELECT min(first_time), max(last_time) FROM devices").fetchone()
            if row[0] is not None:
                return int(row[0]), int(row[1])
    raise ValueError("no capture timestamps were found")


def delete_edge_rows(path, seconds):
    uri = f"file:{path.as_posix()}?mode=rw"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA secure_delete=ON")
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"SQLite quick_check failed: {integrity}")

        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
        if not {"devices", "packets"}.issubset(tables):
            raise ValueError("not a compatible Kismet database")

        start, end = capture_bounds(connection, tables)
        keep_start, keep_end = start + seconds, end - seconds
        if keep_start > keep_end:
            raise ValueError(
                f"capture is only {end - start} seconds; deleting {seconds} seconds "
                "from both ends would leave no interval"
            )

        counts = {}
        connection.execute("BEGIN IMMEDIATE")
        for table in sorted(tables):
            if "ts_sec" not in table_columns(connection, table):
                continue
            cursor = connection.execute(
                f"DELETE FROM {quote_identifier(table)} WHERE ts_sec < ? OR ts_sec > ?",
                (keep_start, keep_end),
            )
            if cursor.rowcount:
                counts[table] = cursor.rowcount

        device_columns = table_columns(connection, "devices")
        if {"first_time", "last_time"}.issubset(device_columns):
            # Device rows summarize their full lifetime. Any overlap means the
            # row may contain an SSID or other information learned at an edge.
            cursor = connection.execute(
                "DELETE FROM devices WHERE first_time < ? OR last_time > ?",
                (keep_start, keep_end),
            )
            if cursor.rowcount:
                counts["devices"] = counts.get("devices", 0) + cursor.rowcount

        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("VACUUM")
        connection.execute("PRAGMA optimize")

    remnants = []
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            if sidecar.stat().st_size == 0:
                sidecar.unlink()
            else:
                remnants.append(str(sidecar))
    if remnants:
        raise ValueError("non-empty SQLite sidecar remains: " + ", ".join(remnants))
    return keep_start, keep_end, counts


def main():
    args = arguments()
    if args.seconds < 0:
        raise SystemExit("--seconds must be zero or greater")
    databases = find_databases(args.paths)
    if not databases:
        raise SystemExit("No .kismet files found")

    failures = 0
    for path in databases:
        if not os.access(path, os.W_OK):
            print(f"ERROR {path}: file is not writable", file=sys.stderr)
            failures += 1
            continue
        try:
            keep_start, keep_end, counts = delete_edge_rows(path, args.seconds)
            summary = ", ".join(f"{table}={count}" for table, count in sorted(counts.items()))
            print(f"DELETED {path}: kept {keep_start}..{keep_end}; {summary or 'no rows'}")
        except (OSError, sqlite3.Error, ValueError) as error:
            print(f"ERROR {path}: {error}", file=sys.stderr)
            failures += 1
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
