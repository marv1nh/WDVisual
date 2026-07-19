# WDVisual

A local, read-only web app for exploring Wardrive and Kismet `.kismet` SQLite databases. It supports search, filters, sorting, statistics, uploads, SSID-grouped Wi-Fi network views, individual radio device views, and a cumulative timeline for packets, devices, and unique SSIDs.

## Features

- Browse mounted `.kismet` files or upload a file through the web UI.
- Switch between Wi-Fi networks grouped by SSID and individual radio devices.
- Inspect access point radios and clients inferred from captured BSSID traffic.
- Search, filter, and sort network and device lists.
- Review packet, device, and SSID trends over time.

## Quick Start

Keep your `.kismet` files in the parent directory, then run this from the `webapp` directory:

```bash
docker compose up --build
```

Open <http://localhost:8080>.

## Storage

- The parent directory is mounted read-only at `/data`.
- Uploaded files are stored separately in the `kismet_uploads` Docker volume.
- The default upload limit is 512 MB and can be changed with `MAX_UPLOAD_MB` in `compose.yaml`.

## Stop

```bash
docker compose down
```

Uploaded files are preserved. Use `docker compose down -v` only when you intentionally want to delete the upload volume as well.
