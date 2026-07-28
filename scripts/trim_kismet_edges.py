#!/usr/bin/env python3
"""Destructively remove the beginning and end of Kismet databases in-place.

This utility deliberately makes no backup.  In addition to deleting timestamped
rows outside the retained interval, it removes every aggregate device row whose
lifetime overlaps either deleted edge.  This prevents SSIDs, MAC addresses,
counts, GPS, or other device metadata learned at an edge from surviving.
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path


DEFAULT_SECONDS = 120
GPS_COLUMNS = (
    "min_lat", "min_lon", "max_lat", "max_lon", "avg_lat", "avg_lon",
)


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths", nargs="+", type=Path,
        help=".kismet files or directories to process (directories are recursive)",
    )
    parser.add_argument(
        "--seconds", type=int, default=DEFAULT_SECONDS,
        help=f"seconds to remove from each end (default: {DEFAULT_SECONDS})",
    )
    return parser.parse_args()


def quote_identifier(value):
    return '"' + value.replace('"', '""') + '"'


def find_databases(paths):
    found = set()
    for path in paths:
        if path.is_dir():
            candidates = path.rglob("*.kismet")
        elif path.is_file():
            candidates = (path,)
        else:
            print(f"warning: not found: {path}", file=sys.stderr)
            continue
        for candidate in candidates:
            if candidate.is_file():
                found.add(candidate.resolve())
    return sorted(found)


def table_columns(connection, table):
    quoted = quote_identifier(table)
    return {row[1] for row in connection.execute(f"PRAGMA table_info({quoted})")}


def capture_bounds(connection, tables):
    # Packets define the actual capture when available.  Empty/packetless logs
    # fall back to every recognized event timestamp and then device lifetimes.
    sources = []
    if "packets" in tables and "ts_sec" in table_columns(connection, "packets"):
        row = connection.execute("SELECT min(ts_sec), max(ts_sec) FROM packets").fetchone()
        if row[0] is not None:
            return int(row[0]), int(row[1])

    for table in tables:
        columns = table_columns(connection, table)
        if "ts_sec" in columns:
            qtable = quote_identifier(table)
            sources.append(f"SELECT ts_sec AS stamp FROM {qtable}")
    if sources:
        row = connection.execute(
            "SELECT min(stamp), max(stamp) FROM (" + " UNION ALL ".join(sources) + ")"
        ).fetchone()
        if row[0] is not None:
            return int(row[0]), int(row[1])

    if "devices" in tables:
        columns = table_columns(connection, "devices")
        if {"first_time", "last_time"}.issubset(columns):
            row = connection.execute(
                "SELECT min(first_time), max(last_time) FROM devices"
            ).fetchone()
            if row[0] is not None:
                return int(row[0]), int(row[1])
    raise ValueError("no capture timestamps were found")


def scrub_location_json(value):
    """Remove location/GPS components from a serialized Kismet device object."""
    if value is None or value == "":
        return value
    was_bytes = isinstance(value, bytes)
    try:
        decoded = value.decode("utf-8") if was_bytes else value
        document = json.loads(decoded)
    except (UnicodeDecodeError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("a device JSON record could not be parsed safely") from error

    def scrub(node):
        if isinstance(node, dict):
            cleaned = {}
            for key, child in node.items():
                normalized = key.casefold()
                leaf = normalized.rsplit(".", 1)[-1]
                if "location" in normalized or "gps" in normalized:
                    continue
                if leaf in {"lat", "lon", "latitude", "longitude", "alt", "altitude"}:
                    continue
                cleaned[key] = scrub(child)
            return cleaned
        if isinstance(node, list):
            return [scrub(child) for child in node]
        return node

    serialized = json.dumps(scrub(document), separators=(",", ":"))
    return serialized.encode("utf-8") if was_bytes else serialized


def trim_database(path, seconds):
    # A SQLite URI avoids accidentally creating a missing path.
    uri = f"file:{path.as_posix()}?mode=rw"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA secure_delete=ON")
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"SQLite quick_check failed: {integrity}")

        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if not {"devices", "packets"}.issubset(tables):
            raise ValueError("not a compatible Kismet database")

        start, end = capture_bounds(connection, tables)
        keep_start, keep_end = start + seconds, end - seconds
        if keep_start > keep_end:
            raise ValueError(
                f"capture is only {end - start} seconds; trimming {seconds} seconds "
                "from both ends would leave no interval"
            )

        deleted = 0
        connection.execute("BEGIN IMMEDIATE")
        for table in sorted(tables):
            columns = table_columns(connection, table)
            if "ts_sec" not in columns:
                continue
            qtable = quote_identifier(table)
            cursor = connection.execute(
                f"DELETE FROM {qtable} WHERE ts_sec < ? OR ts_sec > ?",
                (keep_start, keep_end),
            )
            deleted += max(cursor.rowcount, 0)

        device_columns = table_columns(connection, "devices")
        if {"first_time", "last_time"}.issubset(device_columns):
            # A device row is a denormalized lifetime summary: its SSID, MAC,
            # counters, JSON, and location can incorporate observations from
            # any point between first_time and last_time.  It is not possible
            # to subtract only the edge observations reliably.  Keep only
            # summaries whose complete lifetime is inside the retained window.
            cursor = connection.execute(
                "DELETE FROM devices WHERE first_time < ? OR last_time > ?",
                (keep_start, keep_end),
            )
            deleted += max(cursor.rowcount, 0)

        # These aggregates cannot be accurately reconstructed from packets in
        # every Kismet schema, so clearing them is safer than retaining an edge
        # location.  Per-packet/data GPS inside the retained interval remains.
        present_gps = [column for column in GPS_COLUMNS if column in device_columns]
        if present_gps:
            assignments = ", ".join(
                f"{quote_identifier(column)}=0" for column in present_gps
            )
            connection.execute(f"UPDATE devices SET {assignments}")

        # The device blob duplicates location information in nested JSON.
        # Remove those components too; zeroing only the SQL columns is not safe.
        if "device" in device_columns:
            device_rows = connection.execute(
                "SELECT rowid, device FROM devices WHERE device IS NOT NULL"
            ).fetchall()
            connection.executemany(
                "UPDATE devices SET device=? WHERE rowid=?",
                ((scrub_location_json(blob), rowid) for rowid, blob in device_rows),
            )

        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("VACUUM")
        connection.execute("PRAGMA optimize")

    # SQLite should have consumed these.  Remove empty remnants only; a
    # non-empty remnant indicates something unexpected and is reported.
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
    return start, end, keep_start, keep_end, deleted


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
            _start, _end, keep_start, keep_end, deleted = trim_database(
                path, args.seconds
            )
            print(
                f"TRIMMED {path}: kept {keep_start}..{keep_end}; "
                f"deleted {deleted} timestamped/device rows"
            )
        except (OSError, sqlite3.Error, ValueError) as error:
            print(f"ERROR {path}: {error}", file=sys.stderr)
            failures += 1
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
