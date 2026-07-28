# WDVisual

A local, read-only web app for exploring Wardrive and Kismet `.kismet` SQLite databases. It supports search, filters, sorting, statistics, uploads, SSID-grouped Wi-Fi network views, individual radio device views, and a cumulative timeline for packets, devices, and unique SSIDs.

## Features

- Browse mounted `.kismet` files or upload a file through the web UI.
- Select one or more captures at once to merge stats, timelines, maps, and network/device lists.
- Generate synthetic Wi-Fi captures for feature development without modifying source recordings.
- Switch between Wi-Fi networks grouped by SSID and individual radio devices.
- Inspect access point radios and associated clients using Kismet's last-BSSID
  metadata, with captured BSSID traffic as a fallback.
- Sort networks by their number of inferred associated clients.
- Open an SSID detail drawer with AP coordinates, a dedicated location map, and
  a shortcut that focuses the matching radios on the main map.
- Search, filter, and sort network and device lists.
- Review packet, device, and SSID trends over time.
- Plot GPS-located networks and devices on a detailed OpenStreetMap map with
  pan and zoom controls. Hovering a located Wi-Fi client shows its name (or
  manufacturer when unnamed), with inferred connections drawn to its located
  network access point.
- Switch the observation map between a fast Leaflet street view and an optional
  MapLibre vector view with clustering, pitch, rotation, and close-zoom 3D
  buildings.
- Run versioned session-quality analysis in the background, review confidence
  and limitations, and monitor or cancel persisted jobs.

The staged analysis-platform design and pull-request sequence are documented in
[`docs/feature-plan.md`](docs/feature-plan.md). Module contracts, result
metadata, the quality algorithm and API are documented in
[`docs/analysis-architecture.md`](docs/analysis-architecture.md).

## Quick Start

Keep your `.kismet` files in the parent directory, then run this from the `webapp` directory:

```bash
docker compose up --build
```

Open <http://localhost:8080>.

The default GPS map loads pinned Leaflet assets and OpenStreetMap tiles over the
internet. Optional 3D mode loads pinned MapLibre assets and the OpenFreeMap
Liberty vector style. Map providers receive viewport tile requests, not the
local network/device observation dataset. The rest of the capture viewer
remains local.

## Synthetic capture generator

Open **Generate Data** in the web app to create a new test capture. Choose:

- The number of Wi-Fi networks.
- The number of clients attached to each network. The total device count is `networks × (clients + 1)`.
- An SSID prefix. A prefix of `test` creates `test 1`, `test 2`, and so on.
- Whether to include GPS observations. Pick the center on the map or enter latitude and longitude; observations are distributed throughout a 1 km circle instead of sharing one point.

The generated file is validated, stored in the `kismet_uploads` volume, and selected in the Viewer when it is ready. Generated captures include AP/client packet relationships for the network detail view.

These files implement the schema used by WDVisual. They are development fixtures, not complete Kismet logs intended for import into other Kismet tools.

## Synthetic GPS test data

For an existing disposable capture, the legacy command-line helper can assign deterministic test locations to every device:

```bash
python3 scripts/add_synthetic_gps.py example.kismet --backup /tmp/example-before-gps.kismet
```

This modifies the database in place and must not be used on an original capture without a backup.

## Destructively trim capture edges

To remove the first and last two minutes from one or more captures in-place:

```bash
python3 scripts/trim_kismet_edges.py /path/to/captures
```

Directories are searched recursively for `*.kismet`. This command intentionally
makes no backups. It deletes timestamped rows outside the retained interval,
removes every aggregate device record whose lifetime overlaps either deleted edge,
clears aggregate GPS fields (including location data duplicated in device JSON),
checkpoints SQLite, and vacuums each database. Deleting overlapping device records
is necessary because one row can contain an SSID, MAC address, counters, and other
metadata learned across the device's entire observed lifetime. Close Kismet and
WDVisual before running it. Use `--seconds N` to choose a different duration.

For strict row-only deletion, without modifying any retained row, use:

```bash
python3 scripts/delete_kismet_edge_rows.py /path/to/unprocessed-captures
```

This deletes complete rows from every table with a `ts_sec` column in the first
and last two minutes. It also deletes complete device rows whose summarized
lifetime overlaps either edge, since those rows may contain an SSID or other
metadata learned there. Tables without timestamps cannot be assigned to an edge
and are left unchanged. The operation is in-place and creates no backups.

## Storage

- The parent directory is mounted read-only at `/data`.
- Uploaded and generated files are stored separately in the `kismet_uploads` Docker volume.
- The default upload limit is 512 MB and can be changed with `MAX_UPLOAD_MB` in `compose.yaml`.
- Derived jobs and results are stored separately in the `wdvisual_state` volume.
  Set `WDVISUAL_STATE_DB` to change the application-database path. Removing this
  database does not change capture files; analyses can be regenerated.

## Stop

```bash
docker compose down
```

Uploaded files are preserved. Use `docker compose down -v` only when you intentionally want to delete the upload volume as well.
