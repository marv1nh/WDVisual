# WDVisual

WDVisual is a local web app for exploring wireless network data collected with [Kismet](https://www.kismetwireless.net/). It turns `.kismet` wardriving captures into searchable tables, statistics, timelines, and interactive maps—without changing the original capture files.

## What can you explore?

- **2D and 3D maps** of networks and devices that have GPS coordinates.
- **Wi-Fi networks and radio devices**, including names, MAC addresses, manufacturers, channels, security types, signal strength, and packet activity.
- **Network details** showing observed access points and likely connected clients.
- **Capture timelines** showing when packets, devices, and unique Wi-Fi names were discovered.
- **Multiple captures at once** for a combined view of several wardriving sessions.
- **Search, filters, and sorting** to make larger captures easier to investigate.
- **Session-quality analysis** with a score, confidence information, and notes about missing or limited data.
- **Synthetic test captures** for trying the interface without using real wardriving data.

## What is a `.kismet` file?

Kismet can record observations made while moving through an area with a wireless receiver and, optionally, a GPS device. The resulting `.kismet` file can contain nearby networks, devices, signal levels, packet counts, timestamps, and locations.

WDVisual reads this information and presents it in a more visual format. Captures without GPS data still work; their networks and devices appear in the tables and statistics, but not on the map.

Only collect and use wireless data where you are permitted to do so. Capture files can contain sensitive information such as network names and locations, so review them carefully before sharing.

## Quick start

You need [Docker](https://docs.docker.com/get-docker/) with Docker Compose.

1. Clone the project and enter its directory:

   ```bash
   git clone https://github.com/marv1nh/WDVisual.git
   cd WDVisual
   ```

2. Copy any `.kismet` files you want to browse into the root of the project directory. You can skip this step and upload a capture through the web interface instead.

3. Build and start WDVisual:

   ```bash
   docker compose up --build
   ```

4. Open <http://localhost:8080> in your browser.

Select one or more captures from the file menu to begin exploring them.

## Using WDVisual

### Explore captures

The **Viewer** gives you an overview of the selected captures. Switch between networks grouped by Wi-Fi name and individual radio devices, then use the search and filters to narrow the results. Selecting a network opens a detailed view of its access points, observed clients, and available locations.

The observation map has two modes:

- **2D Street** provides a quick OpenStreetMap view with pan and zoom controls.
- **3D Vector** adds clustering, rotation, pitch, and 3D buildings at close zoom levels. Also the best performing mode when dealing with a lot of data.

When location data is available, WDVisual can also show inferred links between Wi-Fi clients and their access points.

### Check capture quality

The **Data Quality** view analyses a capture in the background and reports how complete and useful the session appears to be. Results include a score, confidence level, explanations, and warnings. The **Analysis Jobs** view lets you follow or cancel this background work.

### Generate test data

The **Generate Data** view creates a synthetic `.kismet` file for development and demonstrations. You can choose:

- How many Wi-Fi networks to create.
- How many clients to attach to each network.
- The prefix used for generated network names.
- Whether to include GPS observations and where to centre them.

Generated locations are spread across a one-kilometre area. These files imitate the parts of the Kismet schema used by WDVisual; they are test fixtures, not full Kismet recordings intended for other tools.

## Privacy and data handling

WDVisual is designed to keep capture processing local:

- Mounted `.kismet` files are opened in SQLite read-only mode and are never migrated or modified.
- The project directory is mounted read-only inside the container.
- Files uploaded through the interface and generated test captures are stored separately in a Docker volume.
- Analysis results are stored in their own Docker volume and can be regenerated.

The maps require an internet connection. The 2D view loads Leaflet and OpenStreetMap tiles, while the optional 3D view loads MapLibre and the OpenFreeMap Liberty style. These providers receive requests for the map area shown in your browser; WDVisual does not send them the network or device records from your capture.

## Configuration

The defaults are defined in [`compose.yaml`](compose.yaml):

| Setting | Default | Purpose |
| --- | --- | --- |
| `KISMET_DATA_DIR` | `/data` | Directory containing mounted `.kismet` files. |
| `KISMET_UPLOAD_DIR` | `/uploads` | Storage for uploaded and generated captures. |
| `WDVISUAL_STATE_DB` | `/state/wdvisual.sqlite3` | Database containing analysis jobs and results. |
| `MAX_UPLOAD_MB` | `512` | Maximum upload size in megabytes. |

The app is exposed on port `8080`. Change the port mapping in `compose.yaml` if that port is already in use.

## Stop or remove WDVisual

Stop the running containers:

```bash
docker compose down
```

Uploaded captures and analysis results remain in their Docker volumes. To remove those volumes too, use the following command only when you intentionally want to delete that stored data:

```bash
docker compose down -v
```

## Development

The backend is a small Flask service in `webapp/app.py`. The dependency-free frontend is served from `webapp/static/`, and SQLite capture access remains read-only.

Useful checks before submitting a change:

```bash
cd webapp
python -m compileall app.py
python -m unittest discover -s tests
node --check static/app.js
```

Node.js is only needed for the JavaScript syntax check, not to run WDVisual.

The legacy `scripts/add_synthetic_gps.py` helper can add deterministic test coordinates to an existing disposable capture. It modifies the database in place, so never run it against an original recording without creating a backup first.

## Source code

WDVisual is available on [GitHub](https://github.com/marv1nh/WDVisual).
