# Analysis architecture

## Storage and read-only boundary

Kismet captures remain the raw, immutable source of truth. Every analysis opens
the selected capture through `app.connect()`, which uses SQLite URI `mode=ro`
and `PRAGMA query_only=ON`.

Jobs and materialized results live in the separate database configured by
`WDVISUAL_STATE_DB`. In Docker this is `/state/wdvisual.sqlite3` on the
`wdvisual_state` volume. Removing that volume removes derived results, not raw
captures; every result can be regenerated.

Migration files are ordered SQL files in `webapp/migrations/`. Applied filenames
are recorded in `schema_migrations`. Migrations must be additive, safe to run in
parallel during a multi-worker startup, and must never attach or update a
capture database.

## Module contract

An analysis module extends `AnalysisModule` and declares:

- stable `name`;
- semantic algorithm `version`;
- `required_tables`;
- user-facing `description`;
- input validation;
- a `run(connection, context, progress)` calculation;
- `ModuleResult(output, confidence, warnings)`.

Register a module instance in `analysis/registry.py`. The store creates an
enabled setting on first discovery, and the module can be disabled through the
module API. Jobs retain the requested version. A worker fails clearly rather
than silently substituting a newer algorithm when that exact version is no
longer installed.

Version changes are required when thresholds, weights, filtering, interpretation
or output semantics change. Documentation and known-outcome tests should change
in the same pull request. Older immutable results remain auditable.

The `AnalysisContext` already uses generic concepts—scope, input revision,
parameters and source path—rather than Wi-Fi-only fields. A future source
adapter can supply another read-only connection or observation reader without
changing result metadata.

## Job lifecycle

`POST /api/analysis/jobs` persists a `queued` job and returns HTTP 202. A daemon
worker claims one job with a short `BEGIN IMMEDIATE` transaction, changes it to
`running`, then releases the application database before scanning the capture.
This makes claiming safe across the configured Gunicorn processes.

Progress updates are small application-database writes. Cancellation sets a
flag; modules encounter it through their progress callback, and completion also
checks it transactionally. Terminal states are `completed`, `failed` and
`cancelled`. Exceptions are stored as bounded error text and shown in Jobs.

Workers start when analysis state is first accessed and resume persisted queued
jobs. Normal capture APIs never wait for analysis. Uploads and generated
captures queue session quality automatically, while the Data Quality view can
queue a fresh run manually.

## Result envelope and invalidation

Every result contains:

- `analysis_type` and `analysis_version`;
- `created_at`;
- `input_scope`;
- `input_revision`;
- `parameters`;
- structured `confidence`;
- `warnings`;
- module-specific `output`.

The current revision is a SHA-256 digest of the opaque capture ID, file size and
nanosecond modification time. This avoids hashing a multi-gigabyte file on each
request. A job fails if the revision changes between queue and execution, and
the latest-result API marks an older result `is_current: false`. Environments
where files can be replaced while preserving both size and modification time
should add optional content hashing in a later version.

## Session quality 1.0.1

The first module reads only `devices` and `packets`, including optional columns
when present. It computes:

- GPS coverage from packet coordinates, falling back to aggregate device
  locations only when packet coordinate columns are unavailable;
- observation density as packets per capture minute on a documented logarithmic
  scale (100 at 60 packets/minute);
- temporal continuity as the share of the capture span outside packet gaps
  longer than 60 seconds;
- a capture-speed component when non-negative packet speed samples exist;
  Kismet stores this field in km/h, while the high-speed threshold is 35 m/s,
  converted to 126 km/h before comparison.

The overall score is the weighted mean of available components. Missing
components are removed from the denominator rather than treated as invented
measurements. Confidence combines measurable component coverage with a
log-scaled sample-size factor and includes human-readable reasons.

Version 1.0.1 corrects the speed unit metadata and labels from m/s to km/h.
Version 1.0.0 results remain immutable and must be rerun to receive the corrected
output contract.

This score measures the fitness of the stored data for later analysis. It does
not prove radio coverage, GPS accuracy or packet loss. Version 1.0.0 explicitly
warns that the required tables do not provide GPS accuracy/satellites,
channel-hopping coverage, source health, repeated coverage or observation
directions. Long gaps are returned as at most 20 aggregate periods, never as
raw route points.

## API

All error responses retain the existing `{"error": "..."}` shape.

- `GET /api/analysis/modules` — module versions, required inputs and enabled
  state.
- `PATCH /api/analysis/modules/<name>` — enable or disable with
  `{"enabled": true|false}`.
- `POST /api/analysis/jobs` — queue one session analysis with
  `{"analysis_type":"session_quality","file":"Mounted:…","parameters":{}}`.
- `GET /api/analysis/jobs?page=1&limit=25&status=running` — bounded job list.
- `GET /api/analysis/jobs/<id>` — progress, error and result ID.
- `POST /api/analysis/jobs/<id>/cancel` — cancel queued/running work.
- `GET /api/analysis/results?page=1&limit=25&analysis_type=…&file=…` —
  filtered, bounded materialized results.
- `GET /api/analysis/results/<id>` — one materialized result.
- `GET /api/analysis/results/latest?analysis_type=session_quality&file=…` —
  latest session result plus current-revision status.

Raw observations are not returned by these endpoints. Scope metadata contains
the locally selected capture ID/name, not filesystem paths or device
identifiers.

## Adding a module

1. Add a focused module under `webapp/analysis/`.
2. Declare inputs and validate schema variants before expensive work.
3. Aggregate in SQL, page or sample large inputs, and report progress at useful
   cancellation boundaries.
4. Return explanations, confidence and limitations with the output.
5. Register the module and add empty, missing-field, known-outcome and
   representative-large-input tests.
6. Document the algorithm, units, thresholds and version policy.
7. Add migrations only when generic result JSON cannot support the required
   indexed query.

## Adding a source type

Keep the source adapter responsible for immutable source discovery, validation,
revision calculation and normalized observation iteration. Do not place
Bluetooth, SDR or sensor-specific columns in the generic jobs/results tables.
Introduce normalized observations only when the first non-Kismet use case
defines its concrete pagination, retention and indexing needs.

## Privacy

This slice is local raw analysis. It adds no public endpoint or export path and
does not store MAC addresses, SSIDs, precise coordinates or raw packet payloads
in analysis metadata. Future export code must require an explicit privacy
profile, project-specific pseudonymization salt and a preview before producing
shareable output. See the staged privacy work in `feature-plan.md`.
