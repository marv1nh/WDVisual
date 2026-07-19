#!/usr/bin/env python3
"""Assign deterministic synthetic GPS coordinates to every Kismet device.

This utility is intended for UI development and test databases only. It updates
the min, max, and average coordinate columns in-place.
"""

import argparse
import math
import shutil
import sqlite3
from pathlib import Path


GOLDEN_ANGLE = math.pi * (3 - math.sqrt(5))
REQUIRED_COLUMNS = {"devkey", "phyname", "devmac", "min_lat", "min_lon",
                    "max_lat", "max_lon", "avg_lat", "avg_lon"}


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="Kismet SQLite database to update")
    parser.add_argument("--latitude", type=float, default=55.6761,
                        help="center latitude (default: Copenhagen)")
    parser.add_argument("--longitude", type=float, default=12.5683,
                        help="center longitude (default: Copenhagen)")
    parser.add_argument("--radius-km", type=float, default=8.0,
                        help="maximum spread from the center in kilometres")
    parser.add_argument("--backup", type=Path,
                        help="optional path for a copy made before modification")
    return parser.parse_args()


def validate(args):
    if not args.database.is_file():
        raise SystemExit(f"Database not found: {args.database}")
    if not -90 <= args.latitude <= 90 or not -180 <= args.longitude <= 180:
        raise SystemExit("Center coordinates are outside valid GPS bounds")
    if args.radius_km <= 0:
        raise SystemExit("--radius-km must be greater than zero")
    if args.backup and args.backup.resolve() == args.database.resolve():
        raise SystemExit("Backup path must differ from the database path")


def location(index, total, latitude, longitude, radius_km):
    """Distribute points evenly in a disc using a deterministic golden spiral."""
    distance = radius_km * math.sqrt((index + 0.5) / max(total, 1))
    angle = index * GOLDEN_ANGLE
    north_km = distance * math.sin(angle)
    east_km = distance * math.cos(angle)
    lat = latitude + north_km / 110.574
    lon_scale = max(0.01, 111.320 * math.cos(math.radians(latitude)))
    lon = longitude + east_km / lon_scale
    return round(lat, 7), round(lon, 7)


def main():
    args = arguments()
    validate(args)
    if args.backup:
        args.backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(args.database, args.backup)
        print(f"Backup: {args.backup}")

    with sqlite3.connect(args.database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(devices)")}
        missing = REQUIRED_COLUMNS - columns
        if missing:
            raise SystemExit(f"Not a compatible Kismet devices table; missing: {', '.join(sorted(missing))}")
        devices = connection.execute(
            "SELECT rowid, devkey, phyname, devmac FROM devices ORDER BY rowid"
        ).fetchall()
        if not devices:
            raise SystemExit("The database contains no devices")
        updates = []
        for index, (rowid, _devkey, _phyname, _devmac) in enumerate(devices):
            lat, lon = location(index, len(devices), args.latitude, args.longitude,
                                args.radius_km)
            updates.append((lat, lon, lat, lon, lat, lon, rowid))
        connection.executemany(
            "UPDATE devices SET min_lat=?,min_lon=?,max_lat=?,max_lon=?,avg_lat=?,avg_lon=? "
            "WHERE rowid=?", updates
        )
        located = connection.execute(
            "SELECT count(*) FROM devices WHERE avg_lat != 0 OR avg_lon != 0"
        ).fetchone()[0]
        if located != len(devices):
            raise RuntimeError(f"Expected {len(devices)} located devices, found {located}")
        connection.commit()
    print(f"Updated {len(devices)} devices within {args.radius_km:g} km of "
          f"{args.latitude:.5f}, {args.longitude:.5f}")


if __name__ == "__main__":
    main()
