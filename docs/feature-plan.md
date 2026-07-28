# WDVisual analysis platform plan

## Current architecture

WDVisual is deliberately small:

- `webapp/app.py` owns capture discovery, upload validation, read-only SQLite
  access, JSON extraction and the JSON API.
- A Kismet file is currently both the session boundary and the source of truth.
  `devices` contains aggregate device records, while `packets` supplies
  timestamped observations. Real Kismet captures can also contain `data`,
  `datasources` and `snapshots`.
- `webapp/static/index.html`, `app.js`, `style.css` and `views.css` form a
  dependency-free single-page UI. The current reusable UI primitives are the
  capture picker, panels, tabs, notices, tables, pagination and the `api()`
  wrapper.
- Existing endpoints expose files, overview counts, devices, SSID groups,
  timelines, locations and inferred clients. Device/network results are
  paginated, but some map and multi-capture paths still collect all matching
  rows in memory.
- Uploaded captures are isolated from mounted captures and every capture is
  opened with SQLite URI `mode=ro` plus `PRAGMA query_only=ON`.

There is no application database, migration runner, analysis contract,
algorithm versioning, persistent result cache or background worker yet.

## Reuse and boundaries

The capture resolver, upload validator, `connect()` read-only guarantee,
Kismet JSON helper and file picker remain unchanged. New code is split into:

- `analysis.base`: module contract, context and structured result type.
- `analysis.registry`: enabled module catalogue and version lookup.
- `analysis.session_quality`: the first domain module.
- `analysis.store`: migrations and persistent jobs/results.
- `analysis.runner`: process-safe job claiming and background execution.
- Flask routes: transport, validation and capture resolution only.

Kismet remains the raw source of truth. Analysis state is kept in a separate
writable SQLite database. This prevents migrations or analysis code from ever
changing a capture and gives later source integrations a neutral persistence
layer.

## First database migration

The initial application database contains:

- `schema_migrations`: applied migration versions.
- `analysis_module_settings`: per-module enable/disable state.
- `analysis_jobs`: scope, requested module/version, input revision,
  parameters, progress, lifecycle timestamps, cancellation and errors.
- `analysis_results`: immutable structured output plus the mandatory analysis
  metadata, confidence and warnings.

Indexes cover job status, module/version, creation time, result scope revision
and result lookup. Jobs reference results, while results retain their input
revision so an older result remains auditable after a capture changes.

Future migrations should add normalized observations or geographic aggregates
only when a feature needs them. Raw Kismet observations must not be copied
without a retention and invalidation reason.

## First vertical slice

`session_quality` version `1.0.0` reads only `devices` and `packets`. It reports
capture duration, device/packet counts, GPS coverage, sampling density,
timestamp continuity, long gaps and speed warnings where those columns exist.
The score is a weighted aggregate of available components; unavailable source
fields are omitted from the denominator. The result explicitly warns when GPS
accuracy, satellites, channel hopping, repeated coverage or direction data
cannot be measured.

Every stored result includes:

- analysis type and version;
- creation time;
- input scope and input revision;
- parameters;
- confidence with reasons;
- warnings;
- structured module output.

The UI adds Data Quality and Jobs views. Running an analysis returns immediately,
the browser polls job state, and completed results are loaded from the result
API. Captures are never sent to the browser as raw observations.

## Pull-request sequence

1. Analysis foundation: application database, migrations, module contract,
   persistent worker, job/module/result APIs and session quality vertical slice.
2. Session detail: richer source health, GPS accuracy/satellite extraction,
   navigable low-quality periods and per-area quality aggregates.
3. Signal observations: normalized read model, paginated time/route history and
   sampling for charts.
4. Position estimates: interchangeable algorithms, outlier filtering,
   uncertainty, explanations and comparison UI.
5. Geographic aggregates: materialized grid/hex cells, zoom-aware heatmap API
   and privacy-safe map layers.
6. Multi-session foundation: stable session/device identities, comparisons,
   coverage overlap and route-aware change confidence.
7. Device history and change detection.
8. Channel, security and separately updateable OUI datasets.
9. Infrastructure relations, bounded graph queries and mobility scoring.
10. Coverage assistant.
11. Privacy profiles, previewable exports and audit metadata.
12. Generic observation contracts and additional source adapters.
13. Routing-provider interface and optional route optimization.

Each PR must keep old APIs working, add migrations and tests with known
synthetic outcomes, document algorithm/version changes, and exercise empty,
missing-GPS and representative large inputs.

## Risks and open questions

- Kismet schemas vary by version. Modules must inspect optional columns and
  degrade with warnings, never assume GPS accuracy or satellite fields exist.
- `devices` is aggregate data; accurate signal history and direction coverage
  require packet/data records and may be incomplete in captures configured not
  to store packets.
- Full scans are acceptable only in background jobs. Later modules need
  bounded SQL aggregation, sampling and materialized geographic cells.
- Multiple Gunicorn workers can race. Job claiming uses a short
  `BEGIN IMMEDIATE` transaction; analysis work occurs outside that transaction.
- SQLite supports the first local deployment well, but high concurrent write
  volume may eventually require a configurable job/result store.
- File size and modification time form the first inexpensive input revision.
  A configurable content hash may be warranted for externally replaced files
  with preserved metadata.
- Quality thresholds are heuristic, versioned and explained. They are not
  calibrated measurement guarantees.
- Local raw analysis can show identifiers already selected by the user. Any
  sharing/export endpoint must require an explicit privacy profile and preview;
  none is added implicitly by this slice.
- Automatic analysis after upload is safe, but mounted captures discovered at
  startup are not automatically queued until an explicit indexing policy is
  defined.
