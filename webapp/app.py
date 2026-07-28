import atexit
import hashlib
import json
import math
import os
import random
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

from analysis import store as analysis_store
from analysis.registry import get_module, modules as analysis_modules
from analysis.runner import JobRunner

app = Flask(__name__, static_folder="static")
DATA_DIR = Path(os.getenv("KISMET_DATA_DIR", "/data"))
UPLOAD_DIR = Path(os.getenv("KISMET_UPLOAD_DIR", "/tmp/wdvisual-uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ANALYSIS_DB = Path(
    os.getenv("WDVISUAL_STATE_DB", str(UPLOAD_DIR / "wdvisual-state.sqlite3"))
)
MIGRATIONS_DIR = Path(__file__).with_name("migrations")
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "512")) * 1024 * 1024
MAX_SYNTHETIC_NETWORKS = 500
MAX_SYNTHETIC_CLIENTS_PER_NETWORK = 100
MAX_SYNTHETIC_DEVICES = 10_000
SYNTHETIC_RADIUS_METRES = 1_000


def available_files():
    found = []
    for root, source in ((DATA_DIR, "Mounted"), (UPLOAD_DIR, "Upload")):
        if not root.exists():
            continue
        for path in sorted(root.glob("*.kismet"), key=lambda p: p.stat().st_mtime, reverse=True):
            found.append({
                "id": f"{source}:{path.name}", "name": path.name, "source": source,
                "size": path.stat().st_size, "modified": int(path.stat().st_mtime),
            })
    return found


def resolve_file(file_id):
    matches = {f["id"]: f for f in available_files()}
    if file_id not in matches:
        raise ValueError("File not found")
    item = matches[file_id]
    root = DATA_DIR if item["source"] == "Mounted" else UPLOAD_DIR
    return root / item["name"], item


def capture_revision(file_id, path):
    stat = path.stat()
    source = f"{file_id}\0{stat.st_size}\0{stat.st_mtime_ns}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def selected_files():
    """Resolve one or more capture IDs from repeated or comma-separated `file` params."""
    ids = request.args.getlist("file")
    if len(ids) == 1 and "," in ids[0]:
        ids = [part.strip() for part in ids[0].split(",") if part.strip()]
    elif not ids:
        raw = request.args.get("file", "").strip()
        ids = [part.strip() for part in raw.split(",") if part.strip()] if raw else []
    if not ids:
        raise ValueError("Select at least one capture file")
    seen, unique = set(), []
    for file_id in ids:
        if file_id not in seen:
            seen.add(file_id)
            unique.append(file_id)
    return [(file_id, *resolve_file(file_id)) for file_id in unique]


def connect(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


_analysis_state_lock = threading.Lock()
_analysis_state_path = None
_analysis_runner = None


def ensure_analysis_state():
    global _analysis_state_path
    current = str(ANALYSIS_DB)
    if _analysis_state_path == current:
        return
    with _analysis_state_lock:
        if _analysis_state_path == current:
            return
        analysis_store.migrate(ANALYSIS_DB, MIGRATIONS_DIR)
        analysis_store.sync_modules(ANALYSIS_DB, analysis_modules())
        _analysis_state_path = current
        analysis_runner().start()


def analysis_runner():
    global _analysis_runner
    if _analysis_runner is None:
        _analysis_runner = JobRunner(
            state_path=lambda: ANALYSIS_DB,
            resolve_capture=resolve_file,
            connect_capture=connect,
            input_revision=capture_revision,
            module_lookup=get_module,
        )
    return _analysis_runner


def stop_analysis_runner():
    global _analysis_runner
    if _analysis_runner is not None:
        _analysis_runner.stop()
        _analysis_runner = None


atexit.register(stop_analysis_runner)


def enqueue_analysis(file_id, module_name="session_quality", parameters=None):
    ensure_analysis_state()
    module = get_module(module_name)
    if module is None:
        raise ValueError("Unknown analysis module")
    if not analysis_store.module_enabled(ANALYSIS_DB, module.name):
        raise ValueError(f"Analysis module {module.name} is disabled")
    path, meta = resolve_file(file_id)
    with connect(path) as connection:
        validation_errors = module.validate(connection)
    if validation_errors:
        raise ValueError("; ".join(validation_errors))
    scope = {
        "type": "session",
        "sessions": [{"id": file_id, "name": meta["name"], "source": meta["source"]}],
    }
    job = analysis_store.create_job(
        ANALYSIS_DB,
        module,
        scope,
        file_id,
        capture_revision(file_id, path),
        parameters or {},
    )
    analysis_runner().wake()
    return job


def automatic_quality_job(file_id):
    try:
        return enqueue_analysis(file_id)
    except (ValueError, sqlite3.Error, OSError) as error:
        app.logger.warning("Automatic session quality analysis was not queued: %s", error)
        return None


def validate_kismet(path):
    try:
        with connect(path) as con:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return {"devices", "packets"}.issubset(tables)
    except sqlite3.Error:
        return False


def synthetic_parameters(payload):
    if not isinstance(payload, dict):
        raise ValueError("Send generation parameters as a JSON object")
    try:
        networks = int(payload.get("networks", 10))
        clients = int(payload.get("clients_per_network", 3))
    except (TypeError, ValueError):
        raise ValueError("Network and client counts must be whole numbers") from None
    if not 1 <= networks <= MAX_SYNTHETIC_NETWORKS:
        raise ValueError(f"Network count must be between 1 and {MAX_SYNTHETIC_NETWORKS}")
    if not 0 <= clients <= MAX_SYNTHETIC_CLIENTS_PER_NETWORK:
        raise ValueError(f"Clients per network must be between 0 and {MAX_SYNTHETIC_CLIENTS_PER_NETWORK}")
    total_devices = networks * (clients + 1)
    if total_devices > MAX_SYNTHETIC_DEVICES:
        raise ValueError(f"Generated captures are limited to {MAX_SYNTHETIC_DEVICES:,} devices")

    prefix = str(payload.get("ssid_prefix", "test")).strip()
    if not prefix:
        raise ValueError("SSID prefix is required")
    if len(prefix) > 32 or any(ord(char) < 32 for char in prefix):
        raise ValueError("SSID prefix must be 32 printable characters or fewer")

    gps_enabled = payload.get("gps_enabled", False)
    if not isinstance(gps_enabled, bool):
        raise ValueError("GPS enabled must be true or false")
    latitude = longitude = 0.0
    if gps_enabled:
        try:
            latitude = float(payload.get("latitude"))
            longitude = float(payload.get("longitude"))
        except (TypeError, ValueError):
            raise ValueError("Valid latitude and longitude are required when GPS is enabled") from None
        if not math.isfinite(latitude) or not -90 <= latitude <= 90:
            raise ValueError("Latitude must be between -90 and 90")
        if not math.isfinite(longitude) or not -180 <= longitude <= 180:
            raise ValueError("Longitude must be between -180 and 180")

    return {
        "networks": networks,
        "clients_per_network": clients,
        "total_devices": total_devices,
        "ssid_prefix": prefix,
        "gps_enabled": gps_enabled,
        "latitude": latitude,
        "longitude": longitude,
    }


def synthetic_mac(index):
    return "02:00:{:02X}:{:02X}:{:02X}:{:02X}".format(
        (index >> 24) & 255, (index >> 16) & 255, (index >> 8) & 255, index & 255
    )


def synthetic_location(latitude, longitude, rng):
    angle = rng.random() * math.tau
    distance = SYNTHETIC_RADIUS_METRES * math.sqrt(rng.random())
    lat = latitude + (distance * math.cos(angle)) / 111_320
    lon_scale = max(0.01, math.cos(math.radians(latitude)))
    lon = longitude + (distance * math.sin(angle)) / (111_320 * lon_scale)
    lon = ((lon + 180) % 360) - 180
    return max(-90, min(90, lat)), lon


def create_synthetic_kismet(path, params, rng=None, now=None):
    rng = rng or random.SystemRandom()
    now = int(now if now is not None else time.time())
    started = now - 3_600
    channels = (1, 6, 11, 36, 44, 149)
    encryptions = ("WPA2 WPA2-PSK AES-CCMP", "WPA3 SAE AES-CCMP", "Open")

    with sqlite3.connect(path) as con:
        con.executescript("""
            PRAGMA journal_mode=DELETE;
            CREATE TABLE devices (
                devkey TEXT PRIMARY KEY,
                phyname TEXT,
                devmac TEXT,
                first_time INTEGER,
                last_time INTEGER,
                strongest_signal INTEGER,
                bytes_data INTEGER,
                type TEXT,
                device TEXT,
                min_lat REAL DEFAULT 0,
                min_lon REAL DEFAULT 0,
                max_lat REAL DEFAULT 0,
                max_lon REAL DEFAULT 0,
                avg_lat REAL DEFAULT 0,
                avg_lon REAL DEFAULT 0
            );
            CREATE TABLE packets (
                ts_sec INTEGER,
                sourcemac TEXT,
                destmac TEXT,
                transmac TEXT
            );
            CREATE INDEX packets_transmac_idx ON packets(transmac);
        """)
        mac_index = 1
        for network_index in range(params["networks"]):
            ssid = f'{params["ssid_prefix"]} {network_index + 1}'
            ap_mac = synthetic_mac(mac_index)
            mac_index += 1
            channel = channels[network_index % len(channels)]
            frequency = 2_412_000 + (channel - 1) * 5_000 if channel <= 14 else 5_000_000 + channel * 5_000
            encryption = encryptions[network_index % len(encryptions)]
            first_time = started + int(network_index * 3_000 / max(1, params["networks"] - 1))
            packet_total = max(1, params["clients_per_network"] * 4)
            signal = -35 - network_index % 55
            if params["gps_enabled"]:
                lat, lon = synthetic_location(params["latitude"], params["longitude"], rng)
            else:
                lat = lon = 0.0
            ap_blob = json.dumps({
                "kismet.device.base.commonname": ssid,
                "kismet.device.base.name": ssid,
                "kismet.device.base.channel": str(channel),
                "kismet.device.base.frequency": frequency,
                "kismet.device.base.crypt": encryption,
                "kismet.device.base.packets.total": packet_total,
                "kismet.device.base.manuf": "WDVisual Synthetic",
            })
            con.execute(
                "INSERT INTO devices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"synthetic-ap-{network_index + 1}", "IEEE802.11", ap_mac, first_time, now,
                 signal, packet_total * 512, "Wi-Fi AP", ap_blob, lat, lon, lat, lon, lat, lon),
            )

            if not params["clients_per_network"]:
                con.execute(
                    "INSERT INTO packets VALUES (?,?,?,?)",
                    (first_time, ap_mac, "FF:FF:FF:FF:FF:FF", ap_mac),
                )
            for client_index in range(params["clients_per_network"]):
                client_mac = synthetic_mac(mac_index)
                mac_index += 1
                client_first = min(now, first_time + 15 + client_index * 7)
                client_signal = max(-95, signal - 5 - client_index % 18)
                if params["gps_enabled"]:
                    client_lat, client_lon = synthetic_location(params["latitude"], params["longitude"], rng)
                else:
                    client_lat = client_lon = 0.0
                client_blob = json.dumps({
                    "kismet.device.base.commonname": f"Client {network_index + 1}-{client_index + 1}",
                    "kismet.device.base.channel": str(channel),
                    "kismet.device.base.frequency": frequency,
                    "kismet.device.base.crypt": encryption,
                    "kismet.device.base.packets.total": 4,
                    "kismet.device.base.manuf": "WDVisual Synthetic",
                    "dot11.device": {
                        "dot11.device.last_bssid": ap_mac,
                    },
                })
                con.execute(
                    "INSERT INTO devices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"synthetic-client-{network_index + 1}-{client_index + 1}", "IEEE802.11",
                     client_mac, client_first, now, client_signal, 2_048, "Wi-Fi Client", client_blob,
                     client_lat, client_lon, client_lat, client_lon, client_lat, client_lon),
                )
                for packet_index in range(4):
                    source, destination = ((client_mac, ap_mac) if packet_index % 2 == 0
                                           else (ap_mac, client_mac))
                    con.execute(
                        "INSERT INTO packets VALUES (?,?,?,?)",
                        (min(now, client_first + packet_index * 30), source, destination, ap_mac),
                    )


@app.post("/api/files/generate")
def generate_file():
    target = None
    try:
        params = synthetic_parameters(request.get_json(silent=True))
        target = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}-synthetic.kismet"
        create_synthetic_kismet(target, params)
        if not validate_kismet(target):
            raise ValueError("The generated file failed Kismet validation")
        stat = target.stat()
        file_id = f"Upload:{target.name}"
        analysis_job = automatic_quality_job(file_id)
        return jsonify(
            ok=True,
            id=file_id,
            name=target.name,
            size=stat.st_size,
            modified=int(stat.st_mtime),
            networks=params["networks"],
            clients=params["networks"] * params["clients_per_network"],
            total_devices=params["total_devices"],
            gps_enabled=params["gps_enabled"],
            analysis_job=analysis_job["id"] if analysis_job else None,
        ), 201
    except (ValueError, sqlite3.Error, OSError) as error:
        if target is not None:
            target.unlink(missing_ok=True)
        return jsonify(error=str(error)), 400


def merge_counts(groups):
    totals = {}
    keyed_by_bucket = False
    for items in groups:
        for item in items:
            if "bucket" in item:
                keyed_by_bucket = True
                key = item["bucket"]
            else:
                key = item["name"]
            totals[key] = totals.get(key, 0) + item["count"]
    if keyed_by_bucket:
        return [{"bucket": key, "count": count} for key, count in sorted(totals.items())]
    return [{"name": key, "count": count} for key, count in sorted(totals.items(), key=lambda x: (-x[1], str(x[0])))]


def capture_fields(file_id, meta):
    return {"file": file_id, "file_name": meta["name"], "file_source": meta["source"]}


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/files")
def files():
    return jsonify(available_files())


@app.post("/api/files")
def upload():
    incoming = request.files.get("file")
    if not incoming or not incoming.filename:
        return jsonify(error="Select a .kismet file"), 400
    name = secure_filename(incoming.filename)
    if not name.lower().endswith(".kismet"):
        return jsonify(error="Only .kismet files are supported"), 400
    target = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}-{name}"
    incoming.save(target)
    if not validate_kismet(target):
        target.unlink(missing_ok=True)
        return jsonify(error="The file is not a valid Kismet SQLite database"), 400
    file_id = f"Upload:{target.name}"
    analysis_job = automatic_quality_job(file_id)
    return jsonify(
        ok=True,
        id=file_id,
        analysis_job=analysis_job["id"] if analysis_job else None,
    ), 201


def json_value(blob, key, default=None):
    try:
        return json.loads(blob or "{}").get(key, default)
    except (ValueError, TypeError):
        return default


def client_last_bssid(blob):
    """Return Kismet's last associated BSSID, excluding empty/zero values."""
    try:
        dot11 = json.loads(blob or "{}").get("dot11.device") or {}
        bssid = str(dot11.get("dot11.device.last_bssid") or "").strip().upper()
        return bssid if bssid and bssid != "00:00:00:00:00:00" else None
    except (ValueError, TypeError, AttributeError):
        return None


def device_summary(row):
    d = dict(row)
    blob = d.pop("device", None)
    d.update(name=json_value(blob, "kismet.device.base.commonname") or json_value(blob, "kismet.device.base.name") or "Unknown",
             channel=json_value(blob, "kismet.device.base.channel", "—"),
             frequency=json_value(blob, "kismet.device.base.frequency"),
             encryption=json_value(blob, "kismet.device.base.crypt", "Unknown"),
             packets=json_value(blob, "kismet.device.base.packets.total", 0),
             manufacturer=json_value(blob, "kismet.device.base.manuf", ""))
    return d


@app.get("/api/overview")
def overview():
    try:
        selected = selected_files()
        total = packets = located = 0
        first = last = None
        phys_groups, type_groups, signal_groups = [], [], []
        for _file_id, path, _meta in selected:
            with connect(path) as con:
                total += con.execute("SELECT count(*) FROM devices").fetchone()[0]
                packets += con.execute("SELECT count(*) FROM packets").fetchone()[0]
                located += con.execute("SELECT count(*) FROM devices WHERE avg_lat != 0 OR avg_lon != 0").fetchone()[0]
                period = con.execute("SELECT min(first_time), max(last_time) FROM devices").fetchone()
                if period[0] is not None:
                    first = period[0] if first is None else min(first, period[0])
                if period[1] is not None:
                    last = period[1] if last is None else max(last, period[1])
                phys_groups.append([dict(r) for r in con.execute(
                    "SELECT phyname name,count(*) count FROM devices GROUP BY phyname ORDER BY count DESC")])
                type_groups.append([dict(r) for r in con.execute(
                    "SELECT type name,count(*) count FROM devices GROUP BY type ORDER BY count DESC")])
                signal_groups.append([dict(r) for r in con.execute(
                    "SELECT ((strongest_signal + 100) / 10) * 10 - 100 bucket,count(*) count "
                    "FROM devices WHERE strongest_signal < 0 GROUP BY bucket ORDER BY bucket")])
        return jsonify(total=total, packets=packets, located=located, first=first, last=last,
                       files=len(selected), phys=merge_counts(phys_groups), types=merge_counts(type_groups),
                       signals=merge_counts(signal_groups))
    except (ValueError, sqlite3.Error) as e:
        return jsonify(error=str(e)), 400


def device_filter_clauses():
    clauses, params = [], []
    search = request.args.get("q", "").strip()
    if search:
        clauses.append("(devmac LIKE ? OR CAST(device AS TEXT) LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    for arg, column in (("phy", "phyname"), ("type", "type")):
        value = request.args.get(arg, "").strip()
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    min_signal = request.args.get("signal", type=int)
    if min_signal is not None:
        clauses.append("strongest_signal >= ?")
        params.append(min_signal)
    if request.args.get("located") == "1":
        clauses.append("(avg_lat != 0 OR avg_lon != 0)")
    encryption = request.args.get("encryption", "").strip()
    if encryption:
        clauses.append("CAST(device AS TEXT) LIKE ?")
        params.append(f'%"kismet.device.base.crypt": "%{encryption}%')
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    sort_map = {"last": "last_time", "first": "first_time", "signal": "strongest_signal", "name": "devmac", "bytes": "bytes_data"}
    sort = sort_map.get(request.args.get("sort"), "last_time")
    direction = "ASC" if request.args.get("dir") == "asc" else "DESC"
    return where, params, sort, direction


@app.get("/api/devices")
def devices():
    try:
        selected = selected_files()
        page = max(1, request.args.get("page", 1, type=int))
        limit = min(200, max(10, request.args.get("limit", 50, type=int)))
        where, params, sort, direction = device_filter_clauses()
        if len(selected) == 1:
            file_id, path, meta = selected[0]
            with connect(path) as con:
                total = con.execute("SELECT count(*) FROM devices" + where, params).fetchone()[0]
                rows = con.execute(
                    f"SELECT * FROM devices{where} ORDER BY {sort} {direction} LIMIT ? OFFSET ?",
                    params + [limit, (page - 1) * limit]).fetchall()
            output = [{**device_summary(row), **capture_fields(file_id, meta)} for row in rows]
            return jsonify(items=output, total=total, page=page, limit=limit, files=1)

        items = []
        for file_id, path, meta in selected:
            with connect(path) as con:
                rows = con.execute(f"SELECT * FROM devices{where} ORDER BY {sort} {direction}", params).fetchall()
            for row in rows:
                items.append({**device_summary(row), **capture_fields(file_id, meta)})
        reverse = direction == "DESC"
        key_map = {"last_time": "last_time", "first_time": "first_time", "strongest_signal": "strongest_signal",
                   "devmac": "devmac", "bytes_data": "bytes_data"}
        sort_key = key_map[sort]

        def sort_value(item):
            value = item.get(sort_key)
            if isinstance(value, str):
                return value.casefold()
            return value if value is not None else (float("-inf") if reverse else float("inf"))

        items.sort(key=sort_value, reverse=reverse)
        total = len(items)
        items = items[(page - 1) * limit:page * limit]
        return jsonify(items=items, total=total, page=page, limit=limit, files=len(selected))
    except (ValueError, sqlite3.Error) as e:
        return jsonify(error=str(e)), 400


@app.get("/api/timeline")
def timeline():
    """Return cumulative packet, device and SSID counts in up to 60 buckets across selected captures."""
    try:
        selected = selected_files()
        bounds_list = []
        for _file_id, path, _meta in selected:
            with connect(path) as con:
                bounds = con.execute(
                    "SELECT min(t),max(t) FROM (SELECT ts_sec t FROM packets UNION ALL SELECT first_time FROM devices)"
                ).fetchone()
            if bounds[0] is not None:
                bounds_list.append((int(bounds[0]), int(bounds[1])))
        if not bounds_list:
            return jsonify(points=[], files=len(selected))
        start = min(b[0] for b in bounds_list)
        end = max(b[1] for b in bounds_list)
        bucket = max(1, (end - start + 59) // 60)
        packets_by, devices_by, ssids_by = {}, {}, {}
        first_ssid = {}
        for _file_id, path, _meta in selected:
            with connect(path) as con:
                for r in con.execute(
                    "SELECT ((ts_sec-?)/?) bucket,count(*) count FROM packets GROUP BY bucket", (start, bucket)
                ):
                    packets_by[int(r[0])] = packets_by.get(int(r[0]), 0) + r[1]
                for r in con.execute(
                    "SELECT ((first_time-?)/?) bucket,count(*) count FROM devices GROUP BY bucket", (start, bucket)
                ):
                    devices_by[int(r[0])] = devices_by.get(int(r[0]), 0) + r[1]
                wifi_rows = con.execute(
                    "SELECT first_time,device FROM devices WHERE phyname='IEEE802.11' AND type='Wi-Fi AP'"
                ).fetchall()
            for r in wifi_rows:
                ssid = (json_value(r["device"], "kismet.device.base.commonname")
                        or json_value(r["device"], "kismet.device.base.name") or "Hidden network")
                key = ssid.casefold()
                first_ssid[key] = min(first_ssid.get(key, r["first_time"]), r["first_time"])
        for ts in first_ssid.values():
            index = max(0, (ts - start) // bucket)
            ssids_by[index] = ssids_by.get(index, 0) + 1
        cumulative_packets = cumulative_devices = cumulative_ssids = 0
        points = []
        for i in range((end - start) // bucket + 1):
            cumulative_packets += packets_by.get(i, 0)
            cumulative_devices += devices_by.get(i, 0)
            cumulative_ssids += ssids_by.get(i, 0)
            points.append({"time": start + i * bucket, "packets": cumulative_packets,
                           "devices": cumulative_devices, "ssids": cumulative_ssids})
        return jsonify(points=points, bucket=bucket, files=len(selected))
    except (ValueError, sqlite3.Error) as e:
        return jsonify(error=str(e)), 400


@app.get("/api/locations")
def locations():
    """Return valid GPS positions from all selected captures."""
    try:
        selected = selected_files()
        items = []
        for file_id, path, meta in selected:
            with connect(path) as con:
                rows = con.execute(
                    "SELECT devmac,phyname,type,avg_lat,avg_lon,strongest_signal,device "
                    "FROM devices WHERE avg_lat BETWEEN -90 AND 90 AND avg_lon BETWEEN -180 AND 180 "
                    "AND NOT (avg_lat = 0 AND avg_lon = 0)"
                ).fetchall()
                ap_macs = {
                    row["devmac"].upper() for row in rows
                    if row["phyname"] == "IEEE802.11" and row["type"] == "Wi-Fi AP"
                }
                association_counts = {}
                if ap_macs:
                    placeholders = ",".join("?" for _ in ap_macs)
                    packet_rows = con.execute(
                        f"SELECT upper(sourcemac) source,upper(destmac) dest,"
                        f"upper(transmac) network_mac,count(*) packets FROM packets "
                        f"WHERE upper(transmac) IN ({placeholders}) "
                        f"GROUP BY source,dest,network_mac",
                        list(ap_macs),
                    ).fetchall()
                    for packet in packet_rows:
                        for client_mac in (packet["source"], packet["dest"]):
                            if (not client_mac or client_mac in ap_macs
                                    or client_mac == "FF:FF:FF:FF:FF:FF"
                                    or client_mac.startswith(("01:", "33:"))):
                                continue
                            key = (client_mac, packet["network_mac"])
                            association_counts[key] = association_counts.get(key, 0) + packet["packets"]
                associations = {}
                for (client_mac, network_mac), packets in association_counts.items():
                    if packets > associations.get(client_mac, (None, -1))[1]:
                        associations[client_mac] = (network_mac, packets)
            for row in rows:
                is_network = row["phyname"] == "IEEE802.11" and row["type"] == "Wi-Fi AP"
                name_candidates = (
                    json_value(row["device"], "kismet.device.base.commonname"),
                    json_value(row["device"], "kismet.device.base.name"),
                )
                normalized_mac = "".join(
                    character for character in (row["devmac"] or "").casefold()
                    if character.isalnum()
                )
                raw_name = next((
                    candidate for candidate in name_candidates
                    if candidate and "".join(
                        character for character in str(candidate).casefold()
                        if character.isalnum()
                    ) != normalized_mac
                ), None)
                manufacturer = json_value(row["device"], "kismet.device.base.manuf", "")
                name = raw_name or ("Hidden network" if is_network else manufacturer or "Unknown device")
                metadata_bssid = client_last_bssid(row["device"])
                client_association = associations.get(row["devmac"].upper())
                items.append({"kind": "network" if is_network else "device", "name": name,
                              "mac": row["devmac"], "type": row["type"] or row["phyname"] or "Unknown",
                              "manufacturer": manufacturer,
                              "network_mac": metadata_bssid or (
                                  client_association[0] if client_association else None
                              ),
                              "lat": row["avg_lat"], "lon": row["avg_lon"],
                              "signal": row["strongest_signal"], **capture_fields(file_id, meta)})
        return jsonify(items=items, total=len(items), files=len(selected))
    except (ValueError, sqlite3.Error) as e:
        return jsonify(error=str(e)), 400


@app.get("/api/network-devices")
def network_devices():
    """Find AP radios and clients for an SSID across selected captures."""
    try:
        selected = selected_files()
        wanted = request.args.get("ssid", "").strip()
        if not wanted:
            return jsonify(error="SSID is required"), 400
        aps, clients_by_mac = [], {}
        for file_id, path, meta in selected:
            with connect(path) as con:
                file_aps = []
                for row in con.execute("SELECT * FROM devices WHERE phyname='IEEE802.11' AND type='Wi-Fi AP'"):
                    summary = {**device_summary(row), **capture_fields(file_id, meta)}
                    if summary["name"].casefold() == wanted.casefold():
                        file_aps.append(summary)
                if not file_aps:
                    continue
                aps.extend(file_aps)
                ap_macs = {a["devmac"].upper() for a in file_aps}
                metadata_bssids = {}
                for row in con.execute(
                        "SELECT * FROM devices WHERE phyname='IEEE802.11' "
                        "AND type='Wi-Fi Client'"):
                    bssid = client_last_bssid(row["device"])
                    if not bssid:
                        continue
                    mac = row["devmac"].upper()
                    metadata_bssids[mac] = bssid
                    if bssid not in ap_macs:
                        continue
                    summary = device_summary(row)
                    if mac in clients_by_mac:
                        client = clients_by_mac[mac]
                        if file_id not in client["files"]:
                            client["files"].append(file_id)
                            client["file_names"].append(meta["name"])
                        client["network_last_time"] = max(
                            client["network_last_time"], summary["last_time"]
                        )
                        continue
                    summary.update(capture_fields(file_id, meta))
                    summary["network_packets"] = 0
                    summary["network_last_time"] = summary["last_time"]
                    summary["files"] = [file_id]
                    summary["file_names"] = [meta["name"]]
                    clients_by_mac[mac] = summary
                placeholders = ",".join("?" for _ in ap_macs)
                packet_rows = con.execute(
                    f"SELECT upper(sourcemac) source,upper(destmac) dest,count(*) packets,max(ts_sec) last_time "
                    f"FROM packets WHERE upper(transmac) IN ({placeholders}) GROUP BY source,dest", list(ap_macs)
                ).fetchall()
                traffic = {}
                for p in packet_rows:
                    for mac in (p["source"], p["dest"]):
                        if (not mac or mac in ap_macs
                                or (mac in metadata_bssids
                                    and metadata_bssids[mac] not in ap_macs)
                                or mac == "FF:FF:FF:FF:FF:FF"
                                or mac.startswith("01:") or mac.startswith("33:33")):
                            continue
                        item = traffic.setdefault(mac, {"packets": 0, "last_time": 0})
                        item["packets"] += p["packets"]
                        item["last_time"] = max(item["last_time"], p["last_time"] or 0)
                if not traffic:
                    continue
                marks = ",".join("?" for _ in traffic)
                known = {r["devmac"].upper(): r for r in con.execute(
                    f"SELECT * FROM devices WHERE upper(devmac) IN ({marks})", list(traffic))}
                for mac, observed in traffic.items():
                    if mac in clients_by_mac:
                        client = clients_by_mac[mac]
                        client["network_packets"] += observed["packets"]
                        client["network_last_time"] = max(client["network_last_time"], observed["last_time"])
                        if file_id not in client["files"]:
                            client["files"].append(file_id)
                            client["file_names"].append(meta["name"])
                        continue
                    client = (device_summary(known[mac]) if mac in known
                              else {"devmac": mac, "name": "Uncatalogued client", "type": "Wi-Fi Client",
                                    "manufacturer": "", "strongest_signal": 0})
                    client.update(capture_fields(file_id, meta))
                    client["network_packets"] = observed["packets"]
                    client["network_last_time"] = observed["last_time"]
                    client["files"] = [file_id]
                    client["file_names"] = [meta["name"]]
                    clients_by_mac[mac] = client
        if not aps:
            return jsonify(error="Network not found"), 404
        clients = sorted(clients_by_mac.values(), key=lambda x: x["network_packets"], reverse=True)
        return jsonify(
            ssid=wanted, access_points=aps, clients=clients, files=len(selected),
            note="Clients use Kismet's last associated BSSID when available, with "
                 "captured BSSID traffic as a fallback; silent clients without "
                 "association metadata may not be identifiable.",
        )
    except (ValueError, sqlite3.Error) as e:
        return jsonify(error=str(e)), 400


@app.get("/api/networks")
def networks():
    """Group Wi-Fi AP radios by advertised network name (SSID) across selected captures."""
    try:
        selected = selected_files()
        page = max(1, request.args.get("page", 1, type=int))
        limit = min(200, max(10, request.args.get("limit", 50, type=int)))
        grouped = {}
        for file_id, path, meta in selected:
            with connect(path) as con:
                rows = con.execute("SELECT * FROM devices WHERE phyname='IEEE802.11' AND type='Wi-Fi AP'").fetchall()
                ap_macs = {row["devmac"].upper() for row in rows}
                clients_by_ap = {mac: set() for mac in ap_macs}
                metadata_bssids = {}
                for client in con.execute(
                        "SELECT devmac,device FROM devices WHERE phyname='IEEE802.11' "
                        "AND type='Wi-Fi Client'"):
                    bssid = client_last_bssid(client["device"])
                    if not bssid:
                        continue
                    client_mac = client["devmac"].upper()
                    metadata_bssids[client_mac] = bssid
                    if bssid in clients_by_ap:
                        clients_by_ap[bssid].add(client_mac)
                if ap_macs:
                    placeholders = ",".join("?" for _ in ap_macs)
                    packet_rows = con.execute(
                        f"SELECT upper(sourcemac) source,upper(destmac) dest,"
                        f"upper(transmac) network_mac FROM packets "
                        f"WHERE upper(transmac) IN ({placeholders}) "
                        f"GROUP BY source,dest,network_mac",
                        list(ap_macs),
                    ).fetchall()
                    for packet in packet_rows:
                        associated = clients_by_ap[packet["network_mac"]]
                        for client_mac in (packet["source"], packet["dest"]):
                            if (not client_mac or client_mac in ap_macs
                                    or (client_mac in metadata_bssids
                                        and metadata_bssids[client_mac]
                                        != packet["network_mac"])
                                    or client_mac == "FF:FF:FF:FF:FF:FF"
                                    or client_mac.startswith(("01:", "33:"))):
                                continue
                            associated.add(client_mac)
            for row in rows:
                d, blob = dict(row), row["device"]
                name = json_value(blob, "kismet.device.base.commonname") or json_value(blob, "kismet.device.base.name") or "Hidden network"
                crypt = json_value(blob, "kismet.device.base.crypt", "Unknown")
                channel = str(json_value(blob, "kismet.device.base.channel", "—"))
                q = request.args.get("q", "").strip().lower()
                if q and q not in name.lower() and q not in d["devmac"].lower() and q not in meta["name"].lower():
                    continue
                encryption = request.args.get("encryption", "").strip().lower()
                if encryption and encryption not in crypt.lower():
                    continue
                min_signal = request.args.get("signal", type=int)
                if min_signal is not None and d["strongest_signal"] < min_signal:
                    continue
                if request.args.get("located") == "1" and not (d["avg_lat"] or d["avg_lon"]):
                    continue
                key = name.casefold()
                if key not in grouped:
                    grouped[key] = {"name": name, "devices": 0, "channels": set(), "encryptions": set(),
                                    "strongest_signal": -999, "packets": 0, "last_time": 0,
                                    "first_time": d["first_time"], "macs": [], "located": 0,
                                    "clients": set(), "files": [], "file_names": []}
                n = grouped[key]
                n["devices"] += 1
                n["clients"].update(clients_by_ap.get(d["devmac"].upper(), set()))
                n["channels"].add(channel)
                n["encryptions"].add(crypt)
                n["strongest_signal"] = max(n["strongest_signal"], d["strongest_signal"])
                n["packets"] += json_value(blob, "kismet.device.base.packets.total", 0) or 0
                n["last_time"] = max(n["last_time"], d["last_time"])
                n["first_time"] = min(n["first_time"], d["first_time"])
                n["macs"].append(d["devmac"])
                n["located"] += int(bool(d["avg_lat"] or d["avg_lon"]))
                if file_id not in n["files"]:
                    n["files"].append(file_id)
                    n["file_names"].append(meta["name"])
        items = list(grouped.values())
        for item in items:
            item["clients"] = len(item["clients"])
        sort_keys = {"devices": "devices", "last": "last_time", "first": "first_time",
                     "signal": "strongest_signal", "name": "name", "packets": "packets",
                     "clients": "clients"}
        sort = sort_keys.get(request.args.get("sort"), "devices")
        items.sort(key=lambda x: str(x[sort]).casefold() if isinstance(x[sort], str) else x[sort],
                   reverse=request.args.get("dir") != "asc")
        total = len(items)
        items = items[(page - 1) * limit:page * limit]
        for n in items:
            n["channels"] = sorted(n["channels"])
            n["encryption"] = " / ".join(sorted(n.pop("encryptions")))
        return jsonify(items=items, total=total, page=page, limit=limit, files=len(selected))
    except (ValueError, sqlite3.Error) as e:
        return jsonify(error=str(e)), 400


@app.get("/api/analysis/modules")
def analysis_module_list():
    try:
        ensure_analysis_state()
        settings = analysis_store.list_module_settings(ANALYSIS_DB)
        return jsonify(items=[
            {
                "analysis_type": module.name,
                "analysis_version": module.version,
                "description": module.description,
                "required_inputs": list(module.required_tables),
                "enabled": settings.get(module.name, True),
            }
            for module in analysis_modules()
        ])
    except (sqlite3.Error, OSError) as error:
        return jsonify(error=str(error)), 400


@app.patch("/api/analysis/modules/<module_name>")
def analysis_module_update(module_name):
    try:
        ensure_analysis_state()
        module = get_module(module_name)
        if module is None:
            return jsonify(error="Analysis module not found"), 404
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
            raise ValueError("enabled must be true or false")
        analysis_store.set_module_enabled(ANALYSIS_DB, module_name, payload["enabled"])
        return jsonify(
            analysis_type=module.name,
            analysis_version=module.version,
            enabled=payload["enabled"],
        )
    except (ValueError, sqlite3.Error, OSError) as error:
        return jsonify(error=str(error)), 400


@app.post("/api/analysis/jobs")
def analysis_job_create():
    try:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise ValueError("Send job parameters as a JSON object")
        file_id = str(payload.get("file", "")).strip()
        if not file_id:
            raise ValueError("Select one session")
        parameters = payload.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ValueError("parameters must be a JSON object")
        analysis_type = str(payload.get("analysis_type", "session_quality")).strip()
        job = enqueue_analysis(file_id, analysis_type, parameters)
        return jsonify(job), 202
    except (ValueError, sqlite3.Error, OSError) as error:
        return jsonify(error=str(error)), 400


@app.get("/api/analysis/jobs")
def analysis_job_list():
    try:
        ensure_analysis_state()
        page = max(1, request.args.get("page", 1, type=int))
        limit = min(100, max(10, request.args.get("limit", 25, type=int)))
        status = request.args.get("status", "").strip() or None
        if status and status not in analysis_store.VALID_STATUSES:
            raise ValueError("Invalid job status")
        analysis_type = request.args.get("analysis_type", "").strip() or None
        if analysis_type and get_module(analysis_type) is None:
            raise ValueError("Unknown analysis module")
        items, total = analysis_store.list_jobs(
            ANALYSIS_DB, page, limit, status, analysis_type
        )
        return jsonify(items=items, total=total, page=page, limit=limit)
    except (ValueError, sqlite3.Error, OSError) as error:
        return jsonify(error=str(error)), 400


@app.get("/api/analysis/jobs/<job_id>")
def analysis_job_detail(job_id):
    try:
        ensure_analysis_state()
        job = analysis_store.get_job(ANALYSIS_DB, job_id)
        if job is None:
            return jsonify(error="Analysis job not found"), 404
        return jsonify(job)
    except (sqlite3.Error, OSError) as error:
        return jsonify(error=str(error)), 400


@app.post("/api/analysis/jobs/<job_id>/cancel")
def analysis_job_cancel(job_id):
    try:
        ensure_analysis_state()
        job = analysis_store.cancel_job(ANALYSIS_DB, job_id)
        if job is None:
            return jsonify(error="Analysis job not found"), 404
        return jsonify(job)
    except (sqlite3.Error, OSError) as error:
        return jsonify(error=str(error)), 400


@app.get("/api/analysis/results/<result_id>")
def analysis_result_detail(result_id):
    try:
        ensure_analysis_state()
        result = analysis_store.get_result(ANALYSIS_DB, result_id)
        if result is None:
            return jsonify(error="Analysis result not found"), 404
        return jsonify(result)
    except (sqlite3.Error, OSError) as error:
        return jsonify(error=str(error)), 400


@app.get("/api/analysis/results")
def analysis_result_list():
    try:
        ensure_analysis_state()
        page = max(1, request.args.get("page", 1, type=int))
        limit = min(100, max(10, request.args.get("limit", 25, type=int)))
        analysis_type = request.args.get("analysis_type", "").strip() or None
        if analysis_type and get_module(analysis_type) is None:
            raise ValueError("Unknown analysis module")
        file_id = request.args.get("file", "").strip() or None
        if file_id:
            resolve_file(file_id)
        items, total = analysis_store.list_results(
            ANALYSIS_DB, page, limit, analysis_type, file_id
        )
        return jsonify(items=items, total=total, page=page, limit=limit)
    except (ValueError, sqlite3.Error, OSError) as error:
        return jsonify(error=str(error)), 400


@app.get("/api/analysis/results/latest")
def analysis_result_latest():
    try:
        ensure_analysis_state()
        file_id = request.args.get("file", "").strip()
        if not file_id:
            raise ValueError("Select one session")
        capture_path, _meta = resolve_file(file_id)
        analysis_type = request.args.get(
            "analysis_type", "session_quality"
        ).strip()
        if get_module(analysis_type) is None:
            raise ValueError("Unknown analysis module")
        result = analysis_store.latest_result(
            ANALYSIS_DB, analysis_type, "session", file_id
        )
        if result is None:
            return jsonify(error="No completed analysis result is available"), 404
        result["is_current"] = (
            result["input_revision"] == capture_revision(file_id, capture_path)
        )
        return jsonify(result)
    except (ValueError, sqlite3.Error, OSError) as error:
        return jsonify(error=str(error)), 400


@app.errorhandler(413)
def too_large(_):
    return jsonify(error="The file exceeds the configured upload limit"), 413
