# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

AeroLake is an RF data-lakehouse pipeline (LASSENA project). It captures radio-frequency
signals, encodes them as [SigMF](https://github.com/sigmf/SigMF) (a `.sigmf-data` binary blob +
a `.sigmf-meta` JSON sidecar), stores them in a MinIO bucket, and reads/validates them back.
Stored captures can be replayed at their recorded cadence and streamed over a ZeroMQ Pub/Sub bus
(ADR-007/008). Real SDR capture and GNU Radio Record/Playback are on the roadmap but **not yet
built** — today the producer generates **synthetic** signals and ingestion is batch.

## Commands

This project uses [uv](https://github.com/astral-sh/uv). Python 3.12+. `src/` layout
(`module-root = src`, so the importable package is `aerolake`, living at `src/aerolake/`).

```bash
uv sync                          # install deps (incl. dev group)

# Start local storage (run from docker/; reads ../.env)
cd docker && docker compose up -d   # MinIO API :9000, console :9001, auto-creates the bucket

# Entry points (defined in pyproject [project.scripts])
uv run aerolake-healthcheck          # verify .env + MinIO reachable + bucket accessible
uv run aerolake-producer --preset gnss-l1 --duration 1.0   # generate+upload a synthetic capture
uv run aerolake-ingest capture.sigmf-data --signal-type gnss_l1 --sample-rate 2e6 --center-freq 1575.42e6  # ingest a REAL IQ file
uv run aerolake-validate --prefix gnss_l1/ --dry-run       # batch-validate a prefix (read-only preview)
uv run aerolake-validate --prefix gnss_l1/ --expected-duration 1.0  # curate: promote quality tags + write reports
uv run aerolake-list --quality validated     # list/filter captures by tag (no byte download)
uv run aerolake-collection --prefix gnss_l1/2026-06-17/ --name "campaign" --description "..."  # group complete Recordings under a prefix into a .sigmf-collection
uv run aerolake-play --prefix gnss_l1/ --start 200 --duration 10   # partial read: t=200s, 10s (HTTP Range)
uv run aerolake-stream --prefix gnss_l1/      # publish a capture's frames over ZeroMQ Pub/Sub
uv run aerolake-subscribe --address tcp://localhost:5555   # subscribe to a ZeroMQ stream (the receiving half, any device)

# Quality / linting / tests
uv run ruff check .              # lint  (ruff config in pyproject; line-length 100, E501 ignored)
uv run ruff format .             # format
uv run mypy src                  # type-check
uv run pytest                    # full suite (verbose + short tracebacks via pyproject addopts)
uv run pytest tests/quality/test_metrics.py            # one file
uv run pytest tests/quality/test_metrics.py::test_name # one test
uv run pytest -k clipping        # tests matching an expression
```

`aerolake-validate` orchestrates `CaptureReader.validate()` over a whole prefix to curate the
bucket (promote `quality` tags, write `quality_report.json` artifacts). `--dry-run` previews
verdicts without mutating anything. `aerolake-list` is the read-only catalog: it lists captures
and filters them by tag (`--signal-type`, `--quality`, `--hardware`, or generic `--tag k=v`)
using only HEAD-class requests (no sample bytes downloaded), per the ADR-003 discovery pattern.
`aerolake-collection` groups every **complete** Recording under a `--prefix` into a single
`.sigmf-collection` (SigMF v1.2.x), written at the prefix root (ADR-014). Orphans (a lone
`.sigmf-data`/`.sigmf-meta`) are skipped but reported; `--dry-run` previews without writing.

## Configuration

Settings come from environment variables prefixed `AEROLAKE_`, loaded via
`aerolake.common.config.Settings` (pydantic-settings). The `.env` at the project root feeds local
dev; real env vars override it. Always read settings through `get_settings()` (it is
`lru_cache`d — `.env` is parsed once per process). Copy `.env.example` to `.env` to start.
`s3_secret_key` is a `SecretStr` so it never leaks into logs/tracebacks.

## Architecture

The pipeline is **Producer → MinIO → Consumer**, with a **Quality** layer that gates what becomes
a "curated" capture. Four packages under `src/aerolake/`:

- **`common/`** — shared infra. `config.py` (Settings). `storage.py` (`StorageClient`, the *single*
  chokepoint for all S3 access; every read/write goes through it — incl. `upload_multipart`
  (streaming upload, ADR-010) and `download_range` (partial reads, ADR-009)).
- **`producer/`** — `synthetic.py` generates IQ samples (`generate_tone`), `sigmf_writer.py`
  encodes them to SigMF bytes (`encode`), `orchestrator.py` (`capture_and_upload`) ties
  generate → encode → upload together. `ingest.py` (`ingest_file`/`ingest_files`,
  `aerolake-ingest`) is the **real-data** entry point: take an existing IQ file **or a directory
  of packet files** (RFSoC `RX0_pkt_*.bin`, concatenated in numeric order; `cf32/cu8/cs16/cs32`
  → normalised cf32), write the `.sigmf-meta`, and stream into MinIO via `upload_multipart`.
  `soapy_source.py` is the real-SDR acquisition layer (SoapySDR): `SdrRecorder` (ADR-015) is an
  **OOP wrapper** owning the device lifecycle (open/configure/start/read/stop/close, context
  manager), with an **injectable `device_opener`** so the whole recorder is testable without
  hardware; `capture_from_sdr` is a thin function shim kept for backward compatibility.
  `gps.py` (`read_geolocation`, ADR-016) reads ONE live fix from gpsd and maps it to a
  SigMF-conformant `core:geolocation` Point (revalidated via `GeolocationConfig`), or returns
  `None` when there is no fix — avoiding the "GPSD trap" (raw dump / `[lat,lon]` order / fake
  position). The gpsd reader is injectable, so the conversion is tested without a daemon.
  `annotations.py` (`AnnotationSegment`, ADR-017) is a **pure** bridge turning GNU Radio cursor
  times (`segment_from_time`) or stream tags (`segments_from_tags`) into SigMF temporal
  annotation segments; `encode(segment_annotations=…)` appends them to the `annotations` array.
- **`consumer/`** — `reader.py` (`CaptureReader`): list/inspect/read captures, `read_segment()`
  for **partial/seeked reads** (HTTP Range — fetch only a `start_s`/`duration_s` window, ADR-009),
  plus `validate()` which runs the quality layer and promotes the capture's quality tag. `player.py`
  (`CapturePlayer`, ADR-007) replays a capture's samples in frames paced at the recorded sample
  rate (injectable clock for tests) — the software half of "playback". `stream.py`
  (`FramePublisher`/`FrameSubscriber`, ADR-008) publishes those frames over a ZeroMQ PUB/SUB bus
  (pure `encode_frame`/`decode_frame` wire format; injectable socket for tests).
- **`quality/`** — `metrics.py` is **pure functions** (no I/O, no logging, no decisions:
  clipping ratio, RMS dBFS, invalid samples, DC offset, completeness, SigMF metadata validity).
  `checker.py` (`QualityChecker`/`QualityReport`) applies configurable `QualityThresholds` to
  those metrics and produces a pass/fail verdict.
`scripts/` holds the CLI entry points (`healthcheck.py`, `producer.py`, `ingest.py`, `validate.py`,
`catalog.py`, `play.py`, `stream.py`, `subscribe.py`), all using `rich` for output and documented exit codes (0 ok / 1 storage failure /
2 config-or-unexpected). All CLIs call `aerolake.common.logging.configure_logging` first so
structlog logs go to stderr, keeping stdout clean for results (`--json`, tables).

### GNU Radio flowgraphs (`gnuradio/`, ADR-007 layer 2/3)

`gnuradio/` holds `record.grc` / `playback.grc` —
**separate from the uv project**: they need a system GNU Radio (`sudo apt install gnuradio`, 3.10+)
and run with the *system* Python that ships its bindings, not `.venv`. The bridge to the rest of
AeroLake is the **`.sigmf-data` file itself**: it is raw `cf32_le`, which GNU Radio's File
Source/Sink read/write natively as *complex* — no SigMF block needed. Validate a `.grc` headlessly
with `grcc -o /tmp gnuradio/playback.grc`; the generated `.py` is gitignored.

> **Note:** the TX flowgraph (`transmit_sdr.grc`), the direct-SDR capture flowgraph
> (`record_sdr.grc`) and the `aerolake-fetch` MinIO→file bridge were **archived** (out of phase-1
> scope, ADR-013) and live on the `archive/explorations-v1` branch. RF transmit is explicitly a
> future phase per the mandate.

### Conventions that span multiple files

These are the load-bearing decisions; read the referenced ADR before changing them.

- **Bucket key layout** (`orchestrator.py`): `{signal_type}/{YYYY-MM-DD}/{session_id}/capture.sigmf-data`
  and `…/capture.sigmf-meta`. `session_id` is 8 hex chars. A capture is "complete" only when both
  objects exist; `CaptureReader.list_captures` skips orphans. Quality reports are written as
  `…/{session}/quality_report.json`.

- **Metadata vs. tags split** (ADR-003, `docs/adr/003-…`): continuous/technical values go in
  `x-amz-meta-*` headers (cheap to read via HEAD, no body transfer); categorical/enumerable values
  go in **S3 tags** (`signal-type`, `recorder`, `hardware`, `quality`) which are indexable and drive
  lifecycle. **Both are attached only to the `.sigmf-data` object** — the `.sigmf-meta` JSON carries
  no headers/tags because its body *is* the description.

- **Upload order matters** (`orchestrator.py`): `.sigmf-meta` is uploaded **before** `.sigmf-data`,
  so a consumer racing between the two puts sees interpretable JSON rather than orphan bytes.

- **`StorageClient.update_tags` is a full REPLACE, not a merge.** The S3 `PutObjectTagging` API
  overwrites the entire tag set. To change one tag (e.g. quality promotion), you MUST read existing
  tags, merge, then write — `CaptureReader.validate` does exactly this. Forgetting the merge wipes
  `signal-type`/`hardware`/`recorder`.

- **Quality lifecycle** (ADR-003 + ADR-004): tag starts at `quality=raw` (set by the producer).
  `validate()` promotes it to `validated` or `rejected` based on the report verdict. `archived` is
  manual. There is no automated retention/lifecycle policy (dropped from scope per ADR-004).

- **boto3 endpoint switch** (ADR-001, `storage.py`): when `s3_endpoint` is empty, boto3 talks to
  real AWS — which is also what **moto** intercepts in tests; when set, it talks to MinIO. MinIO
  needs `signature_version="s3v4"` + path-style addressing (already configured). boto3 (not
  minio-py) was chosen for portability and moto support.

### Project direction (read ADR-013 first)

**ADR-013 realigned the project on the mandate (recadrage, 2026-06-08).** The core deliverable is
the **RX pipeline**: capture → MinIO (SigMF + metadata/tags) → HTTP Range extraction → ZeroMQ
Pub/Sub. An earlier reprioritization toward data quality (ADR-004, after a call with the
supervisor) had deferred the streaming path; ADR-013 **restores streaming as the priority** and
keeps the **quality layer as a support tool**, not the central axis. Out-of-scope explorations
(GUI/ADR-006, `.h5` analysis/ADR-011, TX/ADR-012) were **archived** to the
`archive/explorations-v1` branch. Real SDR capture (SoapySDR) is still future work — today the
producer generates synthetic signals. Trust the ADRs and the code for current status.

## Project context & history

`docs/context/historique-discussions.md` (committed) distills the two desktop-app design
discussions (21–29 May 2026) that predate this repo: the project goal, the 3 demos
(GNSS/Iridium/Starlink), the people (Abdu = project lead, Malek = tutor, Wissem/Ahmad =
receiver owners, Pierre/Lucien = NeSIVA predecessors), the hardware (BladeRF + RTL-SDR,
remote MinIO at fast.etsmtl.ca). Read it for the *why* behind the
code. **Note:** that historical roadmap predates the ADR-013 recadrage; the current direction
follows the mandate's sprint plan (see ADR-013), not the earlier GUI-first roadmap.
Raw transcripts live under `docs/context/transcripts/` (gitignored).

Note: Théo prefers **heavily commented, pedagogical code** — match the existing comment
density in `src/`, it is intentional (a learning aid), not clutter.

## Decision records

`docs/adr/` holds the Architectural Decision Records. They are the authoritative record of *why*
the code is shaped the way it is — consult them before reversing a design choice, and add a new ADR
(don't silently edit an accepted one) when making a decision of comparable weight:

- ADR-001 — boto3 over the MinIO SDK
- ADR-002 — batch upload now, streaming later
- ADR-003 — metadata vs. tagging convention (the key layout + lifecycle)
- ADR-004 — prioritize data quality over streaming (priority later corrected by ADR-013)
- ADR-005 — consumer-side quality tag promotion lifecycle (raw → validated/rejected)
- ADR-006 — *(archived, ADR-013)* visualization GUI: Streamlit + Plotly web app
- ADR-007 — playback strategy (software cadence replay now; SDR re-emission later)
- ADR-008 — ZeroMQ Pub/Sub streaming of capture frames (reactivates ADR-002's streaming half)
- ADR-009 — partial/seeked reads via HTTP Range Requests (Python `read_segment` + GNU Radio offset/length)
- ADR-010 — streaming multipart upload to bypass RAM (`StorageClient.upload_multipart`)
- ADR-011 — *(archived, ADR-013)* analysis viewer for decoded `.h5` tables (GPS/IMU/Iridium)
- ADR-012 — *(archived, ADR-013)* RF re-emission: BladeRF TX flowgraph + MinIO→file bridge
- ADR-013 — **realignment on the mandate** (recadrage): restores the RX→MinIO→ZMQ streaming path as priority, keeps quality as support, archives GUI/analysis/TX
- ADR-014 — SigMF Collections: group complete Recordings under a prefix into a `.sigmf-collection` (prefix selection, relative stream names, orphans reported)
- ADR-015 — OOP `SdrRecorder` wrapper over SoapySDR (device lifecycle as one object; injectable `device_opener` for hardware-free tests; `capture_from_sdr` kept as a shim)
- ADR-016 — SigMF-native geolocation from gpsd (avoid the "GPSD trap": live fix → validated `core:geolocation`, None when no fix; injectable gpsd reader)
- ADR-017 — segment annotations from GNU Radio cursor times / stream tags (pure `annotations.py` bridge → SigMF `annotations`; `encode(segment_annotations=…)`)

## Testing notes

Tests use **moto** to mock S3 (no real MinIO needed). `tests/conftest.py` provides `test_settings`
(isolated from the developer's `.env` by passing values as kwargs, with `s3_endpoint=""` so moto
intercepts), `mock_s3` (a moto-backed client with the test bucket pre-created), and `storage_client`
(a `StorageClient` wired to the mock). Inject these rather than hitting a live backend.

An opt-in **integration test** (`tests/integration/`, marker `integration`) exercises a *real*
MinIO end-to-end (multipart + Range + tagging). It's skipped unless `AEROLAKE_RUN_INTEGRATION=1`;
the CI `integration` job spins up a MinIO container (Docker) and runs `pytest -m integration`.
The producer/ingest declare `core:version` from `sigmf.__specification__` (see
`sigmf_writer.SIGMF_VERSION`), not a hard-coded string.
