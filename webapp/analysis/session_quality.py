import math

from .base import AnalysisContext, AnalysisModule, ModuleResult, ProgressCallback


LONG_GAP_SECONDS = 60
MAX_REPORTED_GAPS = 20
HIGH_SPEED_MPS = 35
HIGH_SPEED_KMH = HIGH_SPEED_MPS * 3.6


def _columns(connection, table):
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _valid_coordinate_clause(latitude, longitude):
    return (
        f"{latitude} BETWEEN -90 AND 90 AND {longitude} BETWEEN -180 AND 180 "
        f"AND NOT ({latitude} = 0 AND {longitude} = 0)"
    )


def _level(score):
    if score >= 80:
        return "high"
    if score >= 55:
        return "medium"
    return "low"


class SessionQualityAnalysis(AnalysisModule):
    name = "session_quality"
    version = "1.0.1"
    required_tables = ("devices", "packets")
    description = (
        "Scores measurable session completeness, GPS coverage, observation "
        "density and timestamp continuity."
    )

    def run(
        self,
        connection,
        context: AnalysisContext,
        progress: ProgressCallback,
    ) -> ModuleResult:
        validation = self.validate(connection)
        if validation:
            raise ValueError("; ".join(validation))

        progress(8)
        device_columns = _columns(connection, "devices")
        packet_columns = _columns(connection, "packets")
        device_count = connection.execute("SELECT count(*) FROM devices").fetchone()[0]
        packet_count = connection.execute("SELECT count(*) FROM packets").fetchone()[0]

        timestamp_sources = []
        if {"first_time", "last_time"}.issubset(device_columns):
            timestamp_sources.append("SELECT first_time t FROM devices WHERE first_time IS NOT NULL")
            timestamp_sources.append("SELECT last_time t FROM devices WHERE last_time IS NOT NULL")
        if "ts_sec" in packet_columns:
            timestamp_sources.append("SELECT ts_sec t FROM packets WHERE ts_sec IS NOT NULL")
        if timestamp_sources:
            bounds = connection.execute(
                "SELECT min(t), max(t) FROM (" + " UNION ALL ".join(timestamp_sources) + ")"
            ).fetchone()
            started_at, ended_at = bounds[0], bounds[1]
        else:
            started_at = ended_at = None
        duration_seconds = max(0, int(ended_at - started_at)) if started_at is not None else 0
        progress(20)

        warnings = []
        components = []

        gps_source = "unavailable"
        located_count = 0
        gps_total = 0
        if {"lat", "lon"}.issubset(packet_columns) and packet_count:
            located_count = connection.execute(
                "SELECT count(*) FROM packets WHERE "
                + _valid_coordinate_clause("lat", "lon")
            ).fetchone()[0]
            gps_total = packet_count
            gps_source = "packet observations"
        elif {"avg_lat", "avg_lon"}.issubset(device_columns) and device_count:
            located_count = connection.execute(
                "SELECT count(*) FROM devices WHERE "
                + _valid_coordinate_clause("avg_lat", "avg_lon")
            ).fetchone()[0]
            gps_total = device_count
            gps_source = "aggregate device locations"

        if gps_total:
            gps_ratio = located_count / gps_total
            components.append({
                "key": "gps_coverage",
                "label": "GPS coverage",
                "score": round(gps_ratio * 100),
                "weight": 0.40,
                "status": _level(gps_ratio * 100),
                "observed": {
                    "located": located_count,
                    "total": gps_total,
                    "ratio": round(gps_ratio, 4),
                    "source": gps_source,
                },
                "explanation": (
                    f"{located_count} of {gps_total} {gps_source} have valid, non-zero coordinates."
                ),
            })
        else:
            gps_ratio = None
            warnings.append("GPS coverage cannot be scored because no usable coordinate records exist.")
        progress(38)

        packets_per_minute = (
            packet_count / max(duration_seconds / 60, 1)
            if packet_count
            else 0
        )
        if packet_count:
            density_score = min(100, round(100 * math.log1p(packets_per_minute) / math.log1p(60)))
            components.append({
                "key": "observation_density",
                "label": "Observation density",
                "score": density_score,
                "weight": 0.25,
                "status": _level(density_score),
                "observed": {"packets_per_minute": round(packets_per_minute, 2)},
                "explanation": (
                    "Passive packet rows per capture minute, scored on a logarithmic scale "
                    "that reaches 100 at 60 packets/minute."
                ),
            })
        else:
            density_score = None
            warnings.append("Observation density cannot be scored because the packet table is empty.")

        gap_count = 0
        longest_gap = 0
        gap_seconds = 0
        low_quality_periods = []
        distinct_seconds = 0
        if "ts_sec" in packet_columns and packet_count:
            gap_summary = connection.execute(
                """
                WITH observed AS (
                    SELECT DISTINCT ts_sec AS t FROM packets WHERE ts_sec IS NOT NULL
                ), gaps AS (
                    SELECT t, t - lag(t) OVER (ORDER BY t) AS gap FROM observed
                )
                SELECT count(*), coalesce(max(gap), 0),
                       coalesce(sum(CASE WHEN gap > ? THEN gap ELSE 0 END), 0)
                FROM gaps WHERE gap > ?
                """,
                (LONG_GAP_SECONDS, LONG_GAP_SECONDS),
            ).fetchone()
            gap_count, longest_gap, gap_seconds = map(int, gap_summary)
            distinct_seconds = connection.execute(
                "SELECT count(DISTINCT ts_sec) FROM packets WHERE ts_sec IS NOT NULL"
            ).fetchone()[0]
            gap_rows = connection.execute(
                """
                WITH observed AS (
                    SELECT DISTINCT ts_sec AS t FROM packets WHERE ts_sec IS NOT NULL
                ), gaps AS (
                    SELECT t, t - lag(t) OVER (ORDER BY t) AS gap FROM observed
                )
                SELECT t - gap AS start, t AS end, gap
                FROM gaps WHERE gap > ? ORDER BY gap DESC LIMIT ?
                """,
                (LONG_GAP_SECONDS, MAX_REPORTED_GAPS),
            ).fetchall()
            low_quality_periods = [
                {"start": int(row[0]), "end": int(row[1]), "duration_seconds": int(row[2])}
                for row in gap_rows
            ]
            if duration_seconds:
                gap_fraction = min(1, gap_seconds / duration_seconds)
                continuity_score = round((1 - gap_fraction) * 100)
            else:
                continuity_score = 100
            components.append({
                "key": "temporal_continuity",
                "label": "Temporal continuity",
                "score": continuity_score,
                "weight": 0.25,
                "status": _level(continuity_score),
                "observed": {
                    "long_gaps": gap_count,
                    "longest_gap_seconds": longest_gap,
                    "gap_seconds": gap_seconds,
                    "threshold_seconds": LONG_GAP_SECONDS,
                },
                "explanation": (
                    "Share of the capture span not contained in packet gaps longer "
                    f"than {LONG_GAP_SECONDS} seconds."
                ),
            })
        else:
            continuity_score = None
            warnings.append("Timestamp continuity cannot be scored without packet timestamps.")
        progress(62)

        speed = {"available": False}
        if "speed" in packet_columns and packet_count:
            speed_row = connection.execute(
                """
                SELECT count(speed), avg(speed), max(speed),
                       sum(CASE WHEN speed > ? THEN 1 ELSE 0 END)
                FROM packets WHERE speed IS NOT NULL AND speed >= 0
                """,
                (HIGH_SPEED_KMH,),
            ).fetchone()
            samples = int(speed_row[0] or 0)
            if samples:
                fast_samples = int(speed_row[3] or 0)
                fast_ratio = fast_samples / samples
                speed_score = round((1 - min(1, fast_ratio * 2)) * 100)
                speed = {
                    "available": True,
                    "unit": "km/h",
                    "samples": samples,
                    "average_kmh": round(speed_row[1] or 0, 2),
                    "maximum_kmh": round(speed_row[2] or 0, 2),
                    "above_threshold": fast_samples,
                    "threshold_kmh": HIGH_SPEED_KMH,
                    "threshold_mps": HIGH_SPEED_MPS,
                }
                components.append({
                    "key": "capture_speed",
                    "label": "Capture speed",
                    "score": speed_score,
                    "weight": 0.10,
                    "status": _level(speed_score),
                    "observed": speed,
                    "explanation": (
                        "Penalises sessions where a large share of recorded GPS speed "
                        f"exceeds {HIGH_SPEED_KMH:g} km/h ({HIGH_SPEED_MPS:g} m/s); "
                        "high speed can reduce useful dwell time."
                    ),
                })

        unavailable = [
            "GPS accuracy and satellite count",
            "channel-hopping coverage",
            "capture-source status and data loss",
            "repeat coverage and observation directions",
        ]
        warnings.append(
            "This version cannot measure " + ", ".join(unavailable) + " from the required tables."
        )

        if components:
            total_weight = sum(item["weight"] for item in components)
            score = round(sum(item["score"] * item["weight"] for item in components) / total_weight)
        else:
            score = 0
            total_weight = 0

        measurable_ratio = total_weight
        sample_factor = min(1, math.log1p(packet_count + device_count) / math.log1p(1000))
        confidence_score = round(100 * (0.7 * measurable_ratio + 0.3 * sample_factor))
        confidence_reasons = [
            f"{len(components)} measurable quality components were available.",
            f"The result is based on {packet_count} packet rows and {device_count} aggregate device rows.",
        ]
        if gps_source == "aggregate device locations":
            confidence_reasons.append(
                "GPS coverage uses aggregate device locations rather than per-packet fixes."
            )
        if packet_count < 20:
            confidence_reasons.append("The small packet sample limits confidence.")

        if score >= 80:
            grade = "good"
        elif score >= 55:
            grade = "mixed"
        else:
            grade = "poor"

        progress(90)
        output = {
            "score": score,
            "grade": grade,
            "summary": (
                f"Session quality is {grade} ({score}/100) across "
                f"{len(components)} measurable components."
            ),
            "metrics": {
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_seconds": duration_seconds,
                "devices": device_count,
                "packets": packet_count,
                "distinct_packet_seconds": distinct_seconds,
                "packets_per_minute": round(packets_per_minute, 2),
                "gps_source": gps_source,
                "speed": speed,
            },
            "components": components,
            "low_quality_periods": low_quality_periods,
            "limitations": unavailable,
        }
        confidence = {
            "score": confidence_score,
            "level": _level(confidence_score),
            "reasons": confidence_reasons,
        }
        progress(100)
        return ModuleResult(output=output, confidence=confidence, warnings=warnings)
