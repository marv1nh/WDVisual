# WDVisual

A local, read-only web app for exploring Wardrive and Kismet `.kismet` SQLite databases. It supports search, filters, sorting, statistics, uploads, SSID-grouped Wi-Fi network views, individual radio device views, and a cumulative timeline for packets, devices, and unique SSIDs.

## Features

- Browse mounted `.kismet` files or upload a file through the web UI.
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

## Synthetic GPS test data

For map development, assign deterministic test locations to every device in a disposable capture database:

```bash
python3 scripts/add_synthetic_gps.py example.kismet --backup /tmp/example-before-gps.kismet
```

This modifies the database in place and must not be used on an original capture without a backup.

## Storage

- The parent directory is mounted read-only at `/data`.
- Uploaded files are stored separately in the `kismet_uploads` Docker volume.
- The default upload limit is 512 MB and can be changed with `MAX_UPLOAD_MB` in `compose.yaml`.

## Stop

```bash
docker compose down
```

Uploaded files are preserved. Use `docker compose down -v` only when you intentionally want to delete the upload volume as well.
