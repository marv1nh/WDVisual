# WDVisual

A local, read-only web app for exploring Wardrive and Kismet `.kismet` SQLite databases. It supports search, filters, sorting, statistics, uploads, SSID-grouped Wi-Fi network views, individual radio device views, and a cumulative timeline for packets, devices, and unique SSIDs.

## Features

- Browse mounted `.kismet` files or upload a file through the web UI.
- Select one or more captures at once to merge stats, timelines, maps, and network/device lists.
- Generate synthetic Wi-Fi captures for feature development without modifying source recordings.
- Switch between Wi-Fi networks grouped by SSID and individual radio devices.
- Inspect access point radios and clients inferred from captured BSSID traffic.
- Search, filter, and sort network and device lists.
- Review packet, device, and SSID trends over time.
- Plot GPS-located networks and devices on a detailed OpenStreetMap map with pan and zoom controls.

## Quick Start

Keep your `.kismet` files in the parent directory, then run this from the `webapp` directory:

```bash
docker compose up --build
```

Open <http://localhost:8080>.

The GPS map loads Leaflet and OpenStreetMap tiles over the internet. The rest of the capture viewer remains local.

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

## Storage

- The parent directory is mounted read-only at `/data`.
- Uploaded and generated files are stored separately in the `kismet_uploads` Docker volume.
- The default upload limit is 512 MB and can be changed with `MAX_UPLOAD_MB` in `compose.yaml`.

## Stop

```bash
docker compose down
```

Uploaded files are preserved. Use `docker compose down -v` only when you intentionally want to delete the upload volume as well.
