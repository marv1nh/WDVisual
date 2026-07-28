import hashlib
import random
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import app as app_module
from analysis.base import AnalysisContext
from analysis.registry import get_module
from analysis import store


class AnalysisTestCase(unittest.TestCase):
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
        app_module.ANALYSIS_DB = self.root / "state" / "wdvisual.sqlite3"
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

    def create_capture(self, name="quality.kismet", gps=True, networks=4, clients=2):
        target = self.data / name
        params = app_module.synthetic_parameters({
            "networks": networks,
            "clients_per_network": clients,
            "ssid_prefix": "known",
            "gps_enabled": gps,
            "latitude": 55.6761,
            "longitude": 12.5683,
        })
        app_module.create_synthetic_kismet(
            target, params, random.Random(12), now=1_700_000_000
        )
        return target, f"Mounted:{name}"

    def wait_for_job(self, job_id, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/analysis/jobs/{job_id}")
            self.assertEqual(response.status_code, 200)
            job = response.get_json()
            if job["status"] in {"completed", "failed", "cancelled"}:
                return job
            time.sleep(0.03)
        self.fail("analysis job did not finish")


class MigrationTests(AnalysisTestCase):
    def test_migration_is_repeatable_and_creates_lookup_indexes(self):
        app_module.ensure_analysis_state()
        app_module._analysis_state_path = None
        app_module.ensure_analysis_state()

        with sqlite3.connect(app_module.ANALYSIS_DB) as connection:
            versions = connection.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
        self.assertEqual(versions, [("0001_analysis_foundation.sql",)])
        self.assertIn("analysis_jobs_status_created_idx", indexes)
        self.assertIn("analysis_results_lookup_idx", indexes)


class SessionQualityUnitTests(AnalysisTestCase):
    def run_module(self, capture, file_id):
        module = get_module("session_quality")
        context = AnalysisContext(
            scope={"type": "session", "sessions": [{"id": file_id}]},
            input_revision="test-revision",
            capture_path=capture,
            parameters={},
        )
        progress = []
        with app_module.connect(capture) as connection:
            result = module.run(connection, context, progress.append)
        return result, progress

    def test_quality_has_explanations_confidence_and_versioned_known_inputs(self):
        capture, file_id = self.create_capture()
        result, progress = self.run_module(capture, file_id)

        self.assertEqual(get_module("session_quality").version, "1.0.1")
        self.assertGreater(result.output["score"], 0)
        self.assertIn(result.output["grade"], {"good", "mixed", "poor"})
        self.assertGreaterEqual(len(result.output["components"]), 2)
        self.assertTrue(all(item["explanation"] for item in result.output["components"]))
        self.assertTrue(result.confidence["reasons"])
        self.assertEqual(progress[-1], 100)
        self.assertIn("GPS accuracy", " ".join(result.warnings))

    def test_missing_gps_is_scored_as_missing_not_as_a_precise_location(self):
        capture, file_id = self.create_capture(gps=False)
        result, _progress = self.run_module(capture, file_id)

        gps = next(
            (item for item in result.output["components"] if item["key"] == "gps_coverage"),
            None,
        )
        self.assertIsNotNone(gps)
        self.assertEqual(gps["score"], 0)
        self.assertEqual(gps["observed"]["located"], 0)

    def test_empty_capture_returns_structured_low_confidence_result(self):
        capture, file_id = self.create_capture(networks=1, clients=0)
        with sqlite3.connect(capture) as connection:
            connection.execute("DELETE FROM packets")
            connection.execute("DELETE FROM devices")
        result, _progress = self.run_module(capture, file_id)

        self.assertEqual(result.output["score"], 0)
        self.assertEqual(result.output["metrics"]["packets"], 0)
        self.assertEqual(result.confidence["level"], "low")
        self.assertGreaterEqual(len(result.warnings), 3)

    def test_large_packet_input_is_aggregated_without_returning_raw_points(self):
        capture, file_id = self.create_capture(networks=1, clients=0)
        with sqlite3.connect(capture) as connection:
            connection.executemany(
                "INSERT INTO packets(ts_sec,sourcemac,destmac,transmac) VALUES (?,?,?,?)",
                [
                    (1_700_000_000 + index, "02:00:00:00:00:01", "FF", "02")
                    for index in range(5_000)
                ],
            )
        result, _progress = self.run_module(capture, file_id)

        self.assertGreaterEqual(result.output["metrics"]["packets"], 5_000)
        self.assertNotIn("observations", result.output)
        self.assertLessEqual(len(result.output["low_quality_periods"]), 20)

    def test_capture_speed_uses_kilometres_per_hour(self):
        capture, file_id = self.create_capture(networks=1, clients=1)
        with sqlite3.connect(capture) as connection:
            connection.execute("ALTER TABLE packets ADD COLUMN speed REAL")
            connection.execute("UPDATE packets SET speed = 54")

        result, _progress = self.run_module(capture, file_id)
        component = next(
            item for item in result.output["components"]
            if item["key"] == "capture_speed"
        )
        speed = component["observed"]

        self.assertEqual(speed["unit"], "km/h")
        self.assertEqual(speed["threshold_kmh"], 126)
        self.assertEqual(speed["threshold_mps"], 35)
        self.assertEqual(speed["average_kmh"], 54)
        self.assertEqual(speed["above_threshold"], 0)
        self.assertEqual(component["score"], 100)
        self.assertNotIn("average_mps", speed)
        self.assertIn("126 km/h (35 m/s)", component["explanation"])


class AnalysisApiTests(AnalysisTestCase):
    def test_job_api_materializes_result_and_does_not_modify_capture(self):
        capture, file_id = self.create_capture()
        before_hash = hashlib.sha256(capture.read_bytes()).hexdigest()
        before_mtime = capture.stat().st_mtime_ns

        created = self.client.post("/api/analysis/jobs", json={
            "analysis_type": "session_quality",
            "file": file_id,
            "parameters": {},
        })
        self.assertEqual(created.status_code, 202)
        job = self.wait_for_job(created.get_json()["id"])
        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertEqual(job["progress"], 100)
        self.assertTrue(job["result_id"])

        result_response = self.client.get(
            "/api/analysis/results/latest",
            query_string={"analysis_type": "session_quality", "file": file_id},
        )
        self.assertEqual(result_response.status_code, 200)
        result = result_response.get_json()
        self.assertEqual(result["analysis_type"], "session_quality")
        self.assertEqual(result["analysis_version"], "1.0.1")
        self.assertEqual(result["input_scope"]["type"], "session")
        self.assertEqual(result["parameters"], {})
        self.assertIn("confidence", result)
        self.assertIn("warnings", result)
        self.assertTrue(result["is_current"])
        listing = self.client.get(
            "/api/analysis/results",
            query_string={"analysis_type": "session_quality", "file": file_id},
        )
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.get_json()["total"], 1)

        self.assertEqual(hashlib.sha256(capture.read_bytes()).hexdigest(), before_hash)
        self.assertEqual(capture.stat().st_mtime_ns, before_mtime)

    def test_jobs_are_paginated_and_modules_can_be_disabled(self):
        _capture, file_id = self.create_capture()
        modules = self.client.get("/api/analysis/modules")
        self.assertEqual(modules.status_code, 200)
        self.assertEqual(modules.get_json()["items"][0]["required_inputs"], ["devices", "packets"])

        disabled = self.client.patch(
            "/api/analysis/modules/session_quality", json={"enabled": False}
        )
        self.assertEqual(disabled.status_code, 200)
        rejected = self.client.post(
            "/api/analysis/jobs",
            json={"analysis_type": "session_quality", "file": file_id},
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("disabled", rejected.get_json()["error"])

        self.client.patch(
            "/api/analysis/modules/session_quality", json={"enabled": True}
        )
        created = self.client.post(
            "/api/analysis/jobs",
            json={"analysis_type": "session_quality", "file": file_id},
        )
        self.assertEqual(created.status_code, 202)
        listing = self.client.get(
            "/api/analysis/jobs", query_string={"page": 1, "limit": 10}
        )
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.get_json()["page"], 1)
        self.assertGreaterEqual(listing.get_json()["total"], 1)

    def test_queued_job_can_be_cancelled(self):
        _capture, file_id = self.create_capture()
        app_module.ensure_analysis_state()
        module = get_module("session_quality")
        path, meta = app_module.resolve_file(file_id)
        queued = store.create_job(
            app_module.ANALYSIS_DB,
            module,
            {"type": "session", "sessions": [{"id": file_id, "name": meta["name"]}]},
            file_id,
            app_module.capture_revision(file_id, path),
            {},
        )

        cancelled = self.client.post(
            f"/api/analysis/jobs/{queued['id']}/cancel"
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.get_json()["status"], "cancelled")

    def test_api_rejects_unknown_session_and_invalid_filters(self):
        unknown = self.client.post(
            "/api/analysis/jobs",
            json={"analysis_type": "session_quality", "file": "Mounted:nope.kismet"},
        )
        self.assertEqual(unknown.status_code, 400)
        invalid = self.client.get(
            "/api/analysis/jobs", query_string={"status": "almost-done"}
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("error", invalid.get_json())


if __name__ == "__main__":
    unittest.main()
