import json
import sqlite3
import time
import uuid
from pathlib import Path


VALID_STATUSES = {"queued", "running", "completed", "failed", "cancelled"}


def _json(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _decode_job(row):
    if row is None:
        return None
    item = dict(row)
    item["input_scope"] = json.loads(item.pop("scope_json"))
    item["parameters"] = json.loads(item.pop("parameters_json"))
    item["cancel_requested"] = bool(item["cancel_requested"])
    return item


def _decode_result(row):
    if row is None:
        return None
    item = dict(row)
    item["input_scope"] = json.loads(item.pop("input_scope_json"))
    item["parameters"] = json.loads(item.pop("parameters_json"))
    item["confidence"] = json.loads(item.pop("confidence_json"))
    item["warnings"] = json.loads(item.pop("warnings_json"))
    item["output"] = json.loads(item.pop("output_json"))
    return item


def connect_state(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def migrate(path, migrations_dir):
    with connect_state(path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at INTEGER NOT NULL
            )
            """
        )
        applied = {
            row[0] for row in connection.execute("SELECT version FROM schema_migrations")
        }
        for migration in sorted(Path(migrations_dir).glob("*.sql")):
            if migration.name in applied:
                continue
            connection.executescript(migration.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (migration.name, int(time.time())),
            )


def sync_modules(path, module_list):
    now = int(time.time())
    with connect_state(path) as connection:
        connection.executemany(
            """
            INSERT INTO analysis_module_settings(module_name, enabled, updated_at)
            VALUES (?, 1, ?)
            ON CONFLICT(module_name) DO NOTHING
            """,
            [(module.name, now) for module in module_list],
        )


def module_enabled(path, name):
    with connect_state(path) as connection:
        row = connection.execute(
            "SELECT enabled FROM analysis_module_settings WHERE module_name = ?",
            (name,),
        ).fetchone()
    return bool(row and row[0])


def list_module_settings(path):
    with connect_state(path) as connection:
        return {
            row["module_name"]: bool(row["enabled"])
            for row in connection.execute(
                "SELECT module_name, enabled FROM analysis_module_settings"
            )
        }


def set_module_enabled(path, name, enabled):
    now = int(time.time())
    with connect_state(path) as connection:
        updated = connection.execute(
            """
            UPDATE analysis_module_settings
            SET enabled = ?, updated_at = ?
            WHERE module_name = ?
            """,
            (int(enabled), now, name),
        )
    return updated.rowcount == 1


def create_job(path, module, scope, scope_key, input_revision, parameters):
    job_id = uuid.uuid4().hex
    now = int(time.time())
    with connect_state(path) as connection:
        connection.execute(
            """
            INSERT INTO analysis_jobs(
                id, module_name, module_version, status, scope_type, scope_key,
                scope_json, input_revision, parameters_json, progress, created_at
            ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                job_id,
                module.name,
                module.version,
                scope["type"],
                scope_key,
                _json(scope),
                input_revision,
                _json(parameters),
                now,
            ),
        )
    return get_job(path, job_id)


def get_job(path, job_id):
    with connect_state(path) as connection:
        row = connection.execute(
            """
            SELECT j.*, r.id AS result_id
            FROM analysis_jobs j
            LEFT JOIN analysis_results r ON r.job_id = j.id
            WHERE j.id = ?
            """,
            (job_id,),
        ).fetchone()
    return _decode_job(row)


def list_jobs(path, page=1, limit=25, status=None, module_name=None):
    clauses = []
    params = []
    if status:
        clauses.append("j.status = ?")
        params.append(status)
    if module_name:
        clauses.append("j.module_name = ?")
        params.append(module_name)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect_state(path) as connection:
        total = connection.execute(
            "SELECT count(*) FROM analysis_jobs j" + where, params
        ).fetchone()[0]
        rows = connection.execute(
            """
            SELECT j.*, r.id AS result_id
            FROM analysis_jobs j
            LEFT JOIN analysis_results r ON r.job_id = j.id
            """
            + where
            + " ORDER BY j.created_at DESC, j.id DESC LIMIT ? OFFSET ?",
            params + [limit, (page - 1) * limit],
        ).fetchall()
    return [_decode_job(row) for row in rows], total


def claim_next_job(path):
    now = int(time.time())
    with connect_state(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT id FROM analysis_jobs
            WHERE status = 'queued' AND cancel_requested = 0
            ORDER BY created_at, id LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        updated = connection.execute(
            """
            UPDATE analysis_jobs SET status = 'running', started_at = ?, progress = 1
            WHERE id = ? AND status = 'queued'
            """,
            (now, row["id"]),
        )
        if updated.rowcount != 1:
            return None
    return get_job(path, row["id"])


def update_progress(path, job_id, progress):
    with connect_state(path) as connection:
        connection.execute(
            """
            UPDATE analysis_jobs SET progress = ?
            WHERE id = ? AND status = 'running'
            """,
            (max(1, min(99, int(progress))), job_id),
        )
        row = connection.execute(
            "SELECT cancel_requested FROM analysis_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    return bool(row and row[0])


def complete_job(path, job, result):
    result_id = uuid.uuid4().hex
    now = int(time.time())
    with connect_state(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT status, cancel_requested FROM analysis_jobs WHERE id = ?",
            (job["id"],),
        ).fetchone()
        if current is None or current["status"] != "running" or current["cancel_requested"]:
            connection.execute(
                """
                UPDATE analysis_jobs
                SET status = 'cancelled', completed_at = ?, progress = 0
                WHERE id = ? AND status = 'running'
                """,
                (now, job["id"]),
            )
            return None
        connection.execute(
            """
            INSERT INTO analysis_results(
                id, job_id, analysis_type, analysis_version, created_at,
                scope_type, scope_key, input_scope_json, input_revision,
                parameters_json, confidence_json, warnings_json, output_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result_id,
                job["id"],
                job["module_name"],
                job["module_version"],
                now,
                job["scope_type"],
                job["scope_key"],
                _json(job["input_scope"]),
                job["input_revision"],
                _json(job["parameters"]),
                _json(result.confidence),
                _json(result.warnings),
                _json(result.output),
            ),
        )
        connection.execute(
            """
            UPDATE analysis_jobs
            SET status = 'completed', progress = 100, completed_at = ?
            WHERE id = ?
            """,
            (now, job["id"]),
        )
    return result_id


def fail_job(path, job_id, error):
    now = int(time.time())
    with connect_state(path) as connection:
        connection.execute(
            """
            UPDATE analysis_jobs
            SET status = CASE WHEN cancel_requested = 1 THEN 'cancelled' ELSE 'failed' END,
                error = CASE WHEN cancel_requested = 1 THEN NULL ELSE ? END,
                completed_at = ?
            WHERE id = ? AND status = 'running'
            """,
            (str(error)[:2000], now, job_id),
        )


def cancel_job(path, job_id):
    now = int(time.time())
    with connect_state(path) as connection:
        row = connection.execute(
            "SELECT status FROM analysis_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        if row["status"] == "queued":
            connection.execute(
                """
                UPDATE analysis_jobs
                SET status = 'cancelled', cancel_requested = 1, completed_at = ?
                WHERE id = ?
                """,
                (now, job_id),
            )
        elif row["status"] == "running":
            connection.execute(
                "UPDATE analysis_jobs SET cancel_requested = 1 WHERE id = ?",
                (job_id,),
            )
    return get_job(path, job_id)


def get_result(path, result_id):
    with connect_state(path) as connection:
        row = connection.execute(
            "SELECT * FROM analysis_results WHERE id = ?", (result_id,)
        ).fetchone()
    return _decode_result(row)


def list_results(path, page=1, limit=25, analysis_type=None, scope_key=None):
    clauses = []
    params = []
    if analysis_type:
        clauses.append("analysis_type = ?")
        params.append(analysis_type)
    if scope_key:
        clauses.append("scope_key = ?")
        params.append(scope_key)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect_state(path) as connection:
        total = connection.execute(
            "SELECT count(*) FROM analysis_results" + where, params
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT * FROM analysis_results"
            + where
            + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params + [limit, (page - 1) * limit],
        ).fetchall()
    return [_decode_result(row) for row in rows], total


def latest_result(path, analysis_type, scope_type, scope_key):
    with connect_state(path) as connection:
        row = connection.execute(
            """
            SELECT * FROM analysis_results
            WHERE analysis_type = ? AND scope_type = ? AND scope_key = ?
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (analysis_type, scope_type, scope_key),
        ).fetchone()
    return _decode_result(row)
