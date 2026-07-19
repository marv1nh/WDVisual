# AGENTS.md

## Project overview

WDVisual is a local, read-only viewer for Wardrive/Kismet `.kismet` SQLite databases. The Flask service in `webapp/app.py` exposes JSON endpoints and serves the dependency-free frontend from `webapp/static/`.

## Repository layout

- `README.md`: user-facing setup and feature documentation.
- `webapp/app.py`: Flask routes, read-only SQLite access, Kismet JSON extraction, and upload validation.
- `webapp/static/index.html`: single-page application markup.
- `webapp/static/app.js`: data loading, filtering, tables, charts, map interaction, and modal behavior.
- `webapp/static/style.css`: shared layout and component styles.
- `webapp/static/views.css`: view-specific styles.
- `webapp/requirements.txt`, `Dockerfile`, and `compose.yaml`: runtime configuration.

## Development rules

- Preserve read-only handling of Kismet databases. Use SQLite URI `mode=ro`, keep `PRAGMA query_only=ON`, and never migrate or modify capture files.
- Keep uploads isolated in `KISMET_UPLOAD_DIR`; validate uploaded databases before making them available.
- Treat database values, filenames, query parameters, and API responses as untrusted. Parameterize SQL values, whitelist SQL identifiers/order clauses, and escape strings inserted into HTML.
- Keep frontend dependencies minimal. The map intentionally loads pinned Leaflet 1.9.4 assets from unpkg and tiles from OpenStreetMap; preserve integrity hashes and required map attribution when changing providers.
- Follow the existing API error shape (`{"error": "..."}`) and convert expected `ValueError`/`sqlite3.Error` failures into HTTP 400 responses.
- Account for databases with no packets, missing JSON keys, hidden SSIDs, zero coordinates, and non-Wi-Fi devices.
- Keep controls keyboard accessible and provide empty/loading/error states for new views.
- Update `README.md` for user-visible features, environment variables, ports, or requirements.

## Running and verification

From `webapp/`, run `docker compose up --build`, then open `http://localhost:8080`.

Before handing off changes:

- Run `python -m compileall app.py` and `node --check static/app.js` when Node is available.
- Exercise affected endpoints with both populated and empty/no-GPS captures when fixtures are available.
- Verify desktop and narrow viewport layouts.
- Do not commit capture databases, uploads, generated caches, secrets, or `__pycache__` files.
