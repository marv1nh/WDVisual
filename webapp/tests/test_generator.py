import json
import math
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module


class SyntheticKismetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.uploads = self.root / "uploads"
        self.data = self.root / "data"
        self.uploads.mkdir()
        self.data.mkdir()
        self.old_upload_dir = app_module.UPLOAD_DIR
        self.old_data_dir = app_module.DATA_DIR
        self.old_analysis_db = app_module.ANALYSIS_DB
        app_module.stop_analysis_runner()
        app_module.UPLOAD_DIR = self.uploads
        app_module.DATA_DIR = self.data
        app_module.ANALYSIS_DB = self.root / "state.sqlite3"
        app_module._analysis_state_path = None
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def tearDown(self):
        app_module.stop_analysis_runner()
        app_module.UPLOAD_DIR = self.old_upload_dir
        app_module.DATA_DIR = self.old_data_dir
        app_module.ANALYSIS_DB = self.old_analysis_db
        app_module._analysis_state_path = None
        self.temp.cleanup()

    def parameters(self, **changes):
        payload = {
            "networks": 3,
            "clients_per_network": 2,
            "ssid_prefix": "test",
            "gps_enabled": True,
            "latitude": 55.6761,
            "longitude": 12.5683,
        }
        payload.update(changes)
        return app_module.synthetic_parameters(payload)

    @staticmethod
    def distance_metres(lat1, lon1, lat2, lon2):
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lon2 - lon1)
        value = (math.sin(delta_phi / 2) ** 2
                 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2)
        return 6_371_000 * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))

    def test_generator_populates_schema_ssids_clients_packets_and_gps(self):
        target = self.uploads / "fixture.kismet"
        params = self.parameters()
        app_module.create_synthetic_kismet(target, params, random.Random(7), now=1_700_000_000)

        self.assertTrue(app_module.validate_kismet(target))
        with sqlite3.connect(target) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM devices").fetchone()[0], 9)
            self.assertEqual(con.execute("SELECT count(*) FROM packets").fetchone()[0], 24)
            rows = con.execute(
                "SELECT type,devmac,avg_lat,avg_lon,device FROM devices ORDER BY devkey"
            ).fetchall()
            ssids = sorted(
                json.loads(blob)["kismet.device.base.commonname"]
                for kind, _mac, _lat, _lon, blob in rows if kind == "Wi-Fi AP"
            )
            self.assertEqual(ssids, ["test 1", "test 2", "test 3"])
            ap_macs = {
                mac for kind, mac, _lat, _lon, _blob in rows if kind == "Wi-Fi AP"
            }
            packet_transmitters = {
                row[0] for row in con.execute("SELECT DISTINCT transmac FROM packets")
            }
            self.assertEqual(packet_transmitters, ap_macs)
            for _kind, _mac, lat, lon, _blob in rows:
                distance = self.distance_metres(55.6761, 12.5683, lat, lon)
                self.assertLessEqual(distance, app_module.SYNTHETIC_RADIUS_METRES + 2)

    def test_generator_uses_zero_coordinates_when_gps_is_disabled(self):
        target = self.uploads / "no-gps.kismet"
        params = self.parameters(gps_enabled=False)
        app_module.create_synthetic_kismet(target, params, random.Random(3), now=1_700_000_000)

        with sqlite3.connect(target) as con:
            located = con.execute(
                "SELECT count(*) FROM devices WHERE avg_lat != 0 OR avg_lon != 0"
            ).fetchone()[0]
        self.assertEqual(located, 0)

    def test_generation_api_returns_discoverable_file_and_client_relationships(self):
        response = self.client.post("/api/files/generate", json={
            "networks": 2,
            "clients_per_network": 3,
            "ssid_prefix": "lab",
            "gps_enabled": True,
            "latitude": 55.6761,
            "longitude": 12.5683,
        })

        self.assertEqual(response.status_code, 201)
        result = response.get_json()
        self.assertEqual(result["total_devices"], 8)
        files = self.client.get("/api/files").get_json()
        self.assertIn(result["id"], {item["id"] for item in files})
        detail = self.client.get(
            "/api/network-devices",
            query_string={"file": result["id"], "ssid": "lab 1"},
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(len(detail.get_json()["clients"]), 3)
        access_point = detail.get_json()["access_points"][0]
        self.assertNotEqual(access_point["avg_lat"], 0)
        self.assertNotEqual(access_point["avg_lon"], 0)
        locations = self.client.get(
            "/api/locations",
            query_string={"file": result["id"]},
        )
        self.assertEqual(locations.status_code, 200)
        located_items = locations.get_json()["items"]
        clients = [item for item in located_items if item["type"] == "Wi-Fi Client"]
        access_point_macs = {
            item["mac"] for item in located_items if item["kind"] == "network"
        }
        self.assertEqual(len(clients), 6)
        self.assertTrue(all(item["name"].startswith("Client ") for item in clients))
        self.assertTrue(all(item["network_mac"] in access_point_macs for item in clients))

    def test_locations_uses_manufacturer_for_unnamed_wifi_client(self):
        target = self.uploads / "manufacturer-fallback.kismet"
        app_module.create_synthetic_kismet(
            target, self.parameters(networks=1, clients_per_network=1),
            random.Random(4), now=1_700_000_000,
        )
        with sqlite3.connect(target) as con:
            row = con.execute(
                "SELECT devkey,devmac,device FROM devices WHERE type='Wi-Fi Client'"
            ).fetchone()
            blob = json.loads(row[2])
            blob["kismet.device.base.commonname"] = row[1]
            blob.pop("kismet.device.base.name", None)
            blob["kismet.device.base.manuf"] = "Fallback Wireless"
            con.execute(
                "UPDATE devices SET device=? WHERE devkey=?",
                (json.dumps(blob), row[0]),
            )

        response = self.client.get(
            "/api/locations",
            query_string={"file": "Upload:manufacturer-fallback.kismet"},
        )

        self.assertEqual(response.status_code, 200)
        client = next(
            item for item in response.get_json()["items"]
            if item["type"] == "Wi-Fi Client"
        )
        self.assertEqual(client["name"], "Fallback Wireless")
        self.assertIsNotNone(client["network_mac"])

    def test_networks_can_sort_by_associated_client_count(self):
        target = self.uploads / "client-sort.kismet"
        app_module.create_synthetic_kismet(
            target, self.parameters(networks=3, clients_per_network=1),
            random.Random(5), now=1_700_000_000,
        )
        with sqlite3.connect(target) as con:
            access_points = con.execute(
                "SELECT devmac FROM devices WHERE type='Wi-Fi AP' ORDER BY devkey"
            ).fetchall()
            first_client = con.execute(
                "SELECT devkey,device FROM devices WHERE type='Wi-Fi Client' ORDER BY devkey"
            ).fetchone()
            first_client_blob = json.loads(first_client[1])
            first_client_blob["dot11.device"]["dot11.device.last_bssid"] = (
                "00:00:00:00:00:00"
            )
            con.execute(
                "UPDATE devices SET device=? WHERE devkey=?",
                (json.dumps(first_client_blob), first_client[0]),
            )
            con.execute("DELETE FROM packets WHERE transmac=?", (access_points[0][0],))
            con.execute(
                "INSERT INTO packets VALUES (?,?,?,?)",
                (1_700_000_000, "02:FF:FF:FF:FF:01",
                 access_points[2][0], access_points[2][0]),
            )

        response = self.client.get(
            "/api/networks",
            query_string={"file": "Upload:client-sort.kismet", "sort": "clients"},
        )

        self.assertEqual(response.status_code, 200)
        items = response.get_json()["items"]
        self.assertEqual(
            [(item["name"], item["clients"]) for item in items],
            [("test 3", 2), ("test 2", 1), ("test 1", 0)],
        )

    def test_last_bssid_associations_work_without_packet_bssids(self):
        target = self.uploads / "last-bssid.kismet"
        app_module.create_synthetic_kismet(
            target, self.parameters(networks=2, clients_per_network=2),
            random.Random(6), now=1_700_000_000,
        )
        with sqlite3.connect(target) as con:
            con.execute("UPDATE packets SET transmac='00:00:00:00:00:00'")

        networks = self.client.get(
            "/api/networks",
            query_string={"file": "Upload:last-bssid.kismet", "sort": "clients"},
        )
        locations = self.client.get(
            "/api/locations",
            query_string={"file": "Upload:last-bssid.kismet"},
        )
        detail = self.client.get(
            "/api/network-devices",
            query_string={"file": "Upload:last-bssid.kismet", "ssid": "test 1"},
        )

        self.assertEqual(networks.status_code, 200)
        self.assertEqual(
            [item["clients"] for item in networks.get_json()["items"]], [2, 2]
        )
        location_items = locations.get_json()["items"]
        ap_macs = {item["mac"] for item in location_items if item["kind"] == "network"}
        clients = [item for item in location_items if item["type"] == "Wi-Fi Client"]
        self.assertTrue(all(item["network_mac"] in ap_macs for item in clients))
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(len(detail.get_json()["clients"]), 2)

    def test_generation_api_rejects_invalid_or_oversized_parameters(self):
        invalid_payloads = [
            {"networks": 0, "clients_per_network": 1, "ssid_prefix": "test", "gps_enabled": False},
            {"networks": 500, "clients_per_network": 100, "ssid_prefix": "test", "gps_enabled": False},
            {"networks": 1, "clients_per_network": 1, "ssid_prefix": "", "gps_enabled": False},
            {"networks": 1, "clients_per_network": 1, "ssid_prefix": "test",
             "gps_enabled": True, "latitude": 91, "longitude": 0},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self.client.post("/api/files/generate", json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.get_json())
        self.assertEqual(list(self.uploads.glob("*.kismet")), [])

    def test_generation_api_removes_file_when_validation_fails(self):
        with patch.object(app_module, "validate_kismet", return_value=False):
            response = self.client.post("/api/files/generate", json={
                "networks": 1,
                "clients_per_network": 0,
                "ssid_prefix": "test",
                "gps_enabled": False,
            })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(list(self.uploads.glob("*.kismet")), [])


if __name__ == "__main__":
    unittest.main()
