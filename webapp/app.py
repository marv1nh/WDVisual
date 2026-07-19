import json
import os
import sqlite3
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder="static")
DATA_DIR = Path(os.getenv("KISMET_DATA_DIR", "/data"))
UPLOAD_DIR = Path(os.getenv("KISMET_UPLOAD_DIR", "/uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "512")) * 1024 * 1024


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
    return root / item["name"]


def connect(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def validate_kismet(path):
    try:
        with connect(path) as con:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return {"devices", "packets"}.issubset(tables)
    except sqlite3.Error:
        return False


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
    return jsonify(ok=True, id=f"Upload:{target.name}"), 201


def json_value(blob, key, default=None):
    try:
        return json.loads(blob or "{}").get(key, default)
    except (ValueError, TypeError):
        return default


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
        path = resolve_file(request.args.get("file", ""))
        with connect(path) as con:
            total = con.execute("SELECT count(*) FROM devices").fetchone()[0]
            packets = con.execute("SELECT count(*) FROM packets").fetchone()[0]
            located = con.execute("SELECT count(*) FROM devices WHERE avg_lat != 0 OR avg_lon != 0").fetchone()[0]
            period = con.execute("SELECT min(first_time), max(last_time) FROM devices").fetchone()
            phys = [dict(r) for r in con.execute("SELECT phyname name,count(*) count FROM devices GROUP BY phyname ORDER BY count DESC")]
            types = [dict(r) for r in con.execute("SELECT type name,count(*) count FROM devices GROUP BY type ORDER BY count DESC")]
            signals = [dict(r) for r in con.execute("SELECT ((strongest_signal + 100) / 10) * 10 - 100 bucket,count(*) count FROM devices WHERE strongest_signal < 0 GROUP BY bucket ORDER BY bucket")]
        return jsonify(total=total, packets=packets, located=located, first=period[0], last=period[1], phys=phys, types=types, signals=signals)
    except (ValueError, sqlite3.Error) as e:
        return jsonify(error=str(e)), 400


@app.get("/api/devices")
def devices():
    try:
        path = resolve_file(request.args.get("file", ""))
        page = max(1, request.args.get("page", 1, type=int))
        limit = min(200, max(10, request.args.get("limit", 50, type=int)))
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
        with connect(path) as con:
            total = con.execute("SELECT count(*) FROM devices" + where, params).fetchone()[0]
            rows = con.execute(f"SELECT * FROM devices{where} ORDER BY {sort} {direction} LIMIT ? OFFSET ?", params + [limit, (page - 1) * limit]).fetchall()
        output = [device_summary(row) for row in rows]
        return jsonify(items=output, total=total, page=page, limit=limit)
    except (ValueError, sqlite3.Error) as e:
        return jsonify(error=str(e)), 400


@app.get("/api/timeline")
def timeline():
    """Return cumulative packet, device and SSID counts in up to 60 buckets."""
    try:
        path = resolve_file(request.args.get("file", ""))
        with connect(path) as con:
            bounds = con.execute("SELECT min(t),max(t) FROM (SELECT ts_sec t FROM packets UNION ALL SELECT first_time FROM devices)").fetchone()
            if bounds[0] is None:
                return jsonify(points=[])
            start, end = int(bounds[0]), int(bounds[1])
            bucket = max(1, (end - start + 59) // 60)
            packet_rows = con.execute("SELECT ((ts_sec-?)/?) bucket,count(*) count FROM packets GROUP BY bucket", (start, bucket)).fetchall()
            device_rows = con.execute("SELECT ((first_time-?)/?) bucket,count(*) count FROM devices GROUP BY bucket", (start, bucket)).fetchall()
            wifi_rows = con.execute("SELECT first_time,device FROM devices WHERE phyname='IEEE802.11' AND type='Wi-Fi AP'").fetchall()
        packets_by = {int(r[0]): r[1] for r in packet_rows}
        devices_by = {int(r[0]): r[1] for r in device_rows}
        first_ssid = {}
        for r in wifi_rows:
            ssid = json_value(r["device"], "kismet.device.base.commonname") or json_value(r["device"], "kismet.device.base.name") or "Hidden network"
            first_ssid[ssid.casefold()] = min(first_ssid.get(ssid.casefold(), r["first_time"]), r["first_time"])
        ssids_by = {}
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
        return jsonify(points=points, bucket=bucket)
    except (ValueError, sqlite3.Error) as e:
        return jsonify(error=str(e)), 400


@app.get("/api/network-devices")
def network_devices():
    """Find AP radios and clients observed exchanging frames through an SSID's BSSIDs."""
    try:
        path = resolve_file(request.args.get("file", ""))
        wanted = request.args.get("ssid", "").strip()
        if not wanted:
            return jsonify(error="SSID is required"), 400
        with connect(path) as con:
            aps = []
            for row in con.execute("SELECT * FROM devices WHERE phyname='IEEE802.11' AND type='Wi-Fi AP'"):
                summary = device_summary(row)
                if summary["name"].casefold() == wanted.casefold():
                    aps.append(summary)
            if not aps:
                return jsonify(error="Network not found"), 404
            ap_macs = {a["devmac"].upper() for a in aps}
            placeholders = ",".join("?" for _ in ap_macs)
            packet_rows = con.execute(
                f"SELECT upper(sourcemac) source,upper(destmac) dest,count(*) packets,max(ts_sec) last_time "
                f"FROM packets WHERE upper(transmac) IN ({placeholders}) GROUP BY source,dest", list(ap_macs)).fetchall()
            traffic = {}
            for p in packet_rows:
                for mac in (p["source"], p["dest"]):
                    if not mac or mac in ap_macs or mac == "FF:FF:FF:FF:FF:FF" or mac.startswith("01:") or mac.startswith("33:33"):
                        continue
                    item = traffic.setdefault(mac, {"packets": 0, "last_time": 0})
                    item["packets"] += p["packets"]
                    item["last_time"] = max(item["last_time"], p["last_time"] or 0)
            clients = []
            if traffic:
                marks = ",".join("?" for _ in traffic)
                known = {r["devmac"].upper(): r for r in con.execute(
                    f"SELECT * FROM devices WHERE upper(devmac) IN ({marks})", list(traffic))}
                for mac, observed in traffic.items():
                    client = device_summary(known[mac]) if mac in known else {"devmac": mac, "name": "Uncatalogued client", "type": "Wi-Fi Client", "manufacturer": "", "strongest_signal": 0}
                    client["network_packets"] = observed["packets"]
                    client["network_last_time"] = observed["last_time"]
                    clients.append(client)
            clients.sort(key=lambda x: x["network_packets"], reverse=True)
        return jsonify(ssid=wanted, access_points=aps, clients=clients,
                       note="Clients are inferred from frames observed through the network BSSID; silent or encrypted-only clients may not be identifiable.")
    except (ValueError, sqlite3.Error) as e:
        return jsonify(error=str(e)), 400


@app.get("/api/networks")
def networks():
    """Group Wi-Fi AP radios by advertised network name (SSID)."""
    try:
        path = resolve_file(request.args.get("file", ""))
        page = max(1, request.args.get("page", 1, type=int))
        limit = min(200, max(10, request.args.get("limit", 50, type=int)))
        with connect(path) as con:
            rows = con.execute("SELECT * FROM devices WHERE phyname='IEEE802.11' AND type='Wi-Fi AP'").fetchall()
        grouped = {}
        for row in rows:
            d, blob = dict(row), row["device"]
            name = json_value(blob, "kismet.device.base.commonname") or json_value(blob, "kismet.device.base.name") or "Hidden network"
            crypt = json_value(blob, "kismet.device.base.crypt", "Unknown")
            channel = str(json_value(blob, "kismet.device.base.channel", "—"))
            q = request.args.get("q", "").strip().lower()
            if q and q not in name.lower() and q not in d["devmac"].lower():
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
                                "first_time": d["first_time"], "macs": [], "located": 0}
            n = grouped[key]
            n["devices"] += 1
            n["channels"].add(channel)
            n["encryptions"].add(crypt)
            n["strongest_signal"] = max(n["strongest_signal"], d["strongest_signal"])
            n["packets"] += json_value(blob, "kismet.device.base.packets.total", 0) or 0
            n["last_time"] = max(n["last_time"], d["last_time"])
            n["first_time"] = min(n["first_time"], d["first_time"])
            n["macs"].append(d["devmac"])
            n["located"] += int(bool(d["avg_lat"] or d["avg_lon"]))
        items = list(grouped.values())
        sort_keys = {"devices": "devices", "last": "last_time", "first": "first_time",
                     "signal": "strongest_signal", "name": "name", "packets": "packets"}
        sort = sort_keys.get(request.args.get("sort"), "devices")
        items.sort(key=lambda x: str(x[sort]).casefold() if isinstance(x[sort], str) else x[sort],
                   reverse=request.args.get("dir") != "asc")
        total = len(items)
        items = items[(page - 1) * limit:page * limit]
        for n in items:
            n["channels"] = sorted(n["channels"])
            n["encryption"] = " / ".join(sorted(n.pop("encryptions")))
        return jsonify(items=items, total=total, page=page, limit=limit)
    except (ValueError, sqlite3.Error) as e:
        return jsonify(error=str(e)), 400


@app.errorhandler(413)
def too_large(_):
    return jsonify(error="The file exceeds the configured upload limit"), 413
