# Slap Biometrics Studio

Python 3.11+ FastAPI workstation for 4–4–2 capture, segmentation, quality gating,
SQLite enrollment, 1:1 verification, and ranked 1:N searches. The dashboard runs
in a desktop browser; the server and database run locally. The complete source
is included. No cloud biometric service is required.

## Run with Docker

Start Docker, then from this directory:

```sh
docker compose up --build
```

Open http://localhost:8000. If port 8000 is already occupied, run
`STUDIO_PORT=8001 docker compose up -d` and open http://localhost:8001. The image builds NBIS from the specified repository,
installs its runtime data, and resolves official SourceAFIS 3.18.1 and transitive
JAR dependencies. It uses Python's Debian Bullseye variant with Python 3.11.
Bullseye's signed archive repositories are configured in a shared base stage;
release-file expiry checks are disabled only for those frozen repositories.
The compose service uses x86-64 for the legacy NBIS build; ARM hosts need Docker
emulation. SourceAFIS is required in Docker; acquisition switches to simulation
when no initialized scanner adapter exists. The database persists in a named volume.

Choose simulation identity `1`, capture right four, segment, enter a name and
enroll. Capture the same identity again and verify against that subject. Change
the identity to exercise a different synthetic pattern. These patterns are for
workflow testing and do not represent real human fingerprints or a validation set.

For ten-print enrollment, select the existing subject, capture left four, segment,
and choose **Add group to selected**; repeat with thumbs. This atomically inserts
or replaces the selected group's fingers. Subject name and simulation provenance
must agree with the existing record.

## Run directly

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.in
MATCHER_ENGINE=demo DEVICE_MODE=simulation python -m app.main
```

This launches at http://127.0.0.1:8000 with an explicitly labeled OpenCV ORB
**demonstration** matcher and estimated quality when NBIS is absent. It has no
published biometric error-rate calibration. ORB points are displayed as features,
never as minutiae. Its scores are not SourceAFIS scores.

For official SourceAFIS locally, install Java 11+ and Maven, then:

```sh
sh scripts/setup_sourceafis.sh
MATCHER_ENGINE=sourceafis python -m app.main
```

The Python module `app/sourceafis.py` exposes extraction and matching through
JPype using the official Java `FingerprintTemplate` / `FingerprintMatcher`
classes. This avoids depending on an unverified Python package named `sourceafis`.
Missing official runtime dependencies fail startup when `MATCHER_ENGINE=sourceafis`.
With `auto`, the dashboard discloses which engine initialized.

## Physical MORPHS / MORPHS-60

**Physical capture is not implemented against a guessed vendor ABI.** The licensed
SDK headers, shared library and scanner were not supplied. The manager attempts
to load `libMorpHs.so` / `MorpHs.dll` for discovery but never invokes speculative
functions. Discovery alone is not proof of working hardware.

`app/hardware_adapter.h` describes the complete, versioned Studio adapter ABI,
with matching ctypes structures in `app/device.py`. This is our ABI, not a claim
about vendor structures. An adapter compiled with verified vendor SDK headers
must export its four functions and normalize scanner pixels/orientation. No
pretend adapter implementation is included. Hardware-only mode fails clearly
without an initialized adapter, and capture errors never substitute a synthetic
frame for a failed physical scan.

On a Linux USB host, put the licensed SDK and compiled adapter in `vendor/morphs`
(`libMorpHs.so` and `libstudio_morphs.so`), then:

```sh
docker compose -f docker-compose.yml -f docker-compose.hardware.yml up --build
```

The override passes through `/dev/bus/usb` and runs as root for vendor USB access;
the default software service runs unprivileged. Docker Desktop on macOS/Windows
does not provide equivalent native USB scanner passthrough. On Windows outside
Docker, set `MORPHS_LIBRARY` and `MORPHS_ADAPTER` to absolute DLL paths; the adapter
uses cdecl. Verify the actual sensor's frame format and dimensions with its SDK.

## Processing semantics

- Acquisitions are 1600×1500 grayscale at an assumed 500 DPI. DPI metadata in an
  uploaded PNG is not treated as proof of calibrated sensor resolution.
- Canonical left-to-right order is right index/middle/ring/little, left
  little/ring/middle/index, and left/right thumbs. Anatomical labeling assumes
  that documented platen orientation, rather than attempting hand inference.
- NFSEG uses documented plain-image syntax, a high-quality baseline JPEG input,
  raw segmented output, and stdout geometry. Timeout, error flags, invalid
  dimensions or wrong finger counts trigger the labeled OpenCV fallback.
- OpenCV bridges ridges using Otsu thresholding and morphology, requires the
  exact expected component count, isolates and straightens minimum-area
  rectangles, and adds 20px margins. Missing or touching fingers require a
  recapture; the application does not manufacture extra finger segments.
- NFIQ 1 is invoked with raw grayscale dimensions and 500 PPI. Valid CLI results
  are 1–5. Failed/missing CLI scoring uses uncalibrated gradient/Laplacian
  estimates, explicitly flagged in the response and UI. These are not NFIQ 2.
- Enrollment and probes require every requested finger's quality score ≤3.
  `ALLOW_ESTIMATED_QUALITY=0` disables enrollment based on estimates.
- Single probes must specify an anatomical finger name. Slap probes compare
  corresponding anatomical positions and use the **minimum** finger score.
  Every requested finger must exist and pass threshold 40 by default. This
  conservative fusion is an application rule, not a calibrated multi-finger
  decision model. Search returns matching subjects ranked by this score.
- Simulator captures only match simulated registry records via the API.
  Uploaded images are unverified and may search all records; uploads cannot
  establish device provenance or liveness. The database's simulated flag is
  informational and not a security proof.
- Original PNGs and templates are stored together. Templates carry their engine
  version. A mismatched engine causes re-extraction from the original PNG, never
  cross-format matching. Searches are exhaustive SQLite-backed scans suitable
  for a small workstation, not a distributed or indexed large-scale ABIS.

## REST API

`GET /api/health` returns active engine and device diagnostics.

```sh
curl -X POST http://localhost:8000/api/capture \
  -H 'Content-Type: application/json' \
  -d '{"mode":"right4","identity":1,"skin":"normal","impression":0}'
```

Use the returned `capture_id` in subsequent JSON bodies. Captures are RAM-only,
expire after 15 minutes and the store retains at most 16. Alternatively provide
`image` as strict base64 PNG (or `data:image/png;base64,...`), omitting `capture_id`.

| Endpoint | JSON body | Result |
| --- | --- | --- |
| `POST /api/capture` | `mode`, optional simulation `identity`, `skin`, `impression` | base64 image, capture id, acquisition metadata |
| `POST /api/segment` | `capture_id` or `image`, `mode` | fingers, boxes, quality, actual minutiae or demo feature counts |
| `POST /api/enroll` | image reference, `mode`, `name`, optional existing `subject_id` | enrollment identity and count |
| `POST /api/verify` | image reference, `mode`, `kind: "1:1"`, `subject_id`, optional threshold | score, decision, per-finger comparisons |
| `POST /api/verify` | image reference, `mode`, `kind: "1:N"`, optional threshold | ranked candidate hits |
| `GET /api/subjects` | none | subject registry and finger counts |
| `GET /api/fingerprints/{id}/image` | none | enrolled PNG for comparison |

For a single-finger probe, use `mode: "single"`, an uploaded finger PNG and
`finger_name`, e.g. `"Right index"`. Capture/segment/enroll modes are `right4`,
`left4`, `thumbs`. Bad data returns 422, missing records 404, busy processing
429, device/runtime failures 503. Requests are limited to 12 MiB and PNGs to
8 MiB / 4 million pixels. See `/docs` for schemas.

## Configuration and deployment limits

`API_TOKEN` enables constant-time bearer-token authentication for every `/api`
route. Enter the token in the dashboard (held in the current page only), or send
`Authorization: Bearer ...`. The default compose port binds loopback. Configure
TLS and authentication before exposing the service remotely. `ALLOWED_ORIGINS`
adds a comma-separated explicit allowlist for browser write origins.

Other settings: `BIOMETRICS_DB`, `DEVICE_MODE=auto|simulation|hardware`,
`MATCHER_ENGINE=auto|sourceafis|demo`, `ALLOW_ESTIMATED_QUALITY=0|1`,
`SOURCEAFIS_CLASSPATH` (JAR glob), `MORPHS_LIBRARY`, `MORPHS_ADAPTER`.

SQLite foreign keys, WAL, parameterized queries and atomic enrollment are used.
The registry contains sensitive image blobs; it is not encrypted at rest. The
service does not provide liveness detection, operator roles, audit trails,
retention controls, identity proofing or production biometric error-rate testing.
Tailwind's CDN is requested by the dashboard; full local CSS also allows the
interface to operate when offline. Use one Uvicorn worker because capture state
and device ownership are process-local.

FAP60 / FBI Appendix F certification applies to tested acquisition systems;
this code does not certify hardware or itself. It does not encode or validate
ISO/IEC 19794-4 interchange records. A production deployment requires its SDK
integration, actual device qualification and application-specific validation.

## Tests

```sh
pytest -q
```

The suite checks all capture modes, anatomical ordering, segmentation failure,
quality rejection with no partial enrollment, ten-print updates, restart
persistence, genuine/impostor synthetic comparisons, API flow, input limits and
authentication. The real SourceAFIS test skips if Java/JARs are unavailable.
To run inside the built image with its real dependencies:

```sh
docker compose run --rm -v "$PWD/tests:/studio/tests:ro" \
  -v "$PWD/pytest.ini:/studio/pytest.ini:ro" studio python -m pytest -q
```

Build and runtime validation status for the delivered environment is recorded in
`VALIDATION.md`; test presence alone is not a statement that tests passed.

## Upstream references

- [Official SourceAFIS Java API and persistence](https://sourceafis.machinezoo.com/java)
- [SourceAFIS template format](https://sourceafis.machinezoo.com/template)
- [NBIS build instructions](https://github.com/biometric-technologies/nist-biometric-image-software-nbis/blob/master/INSTALL_LINUX_MACOSX.txt)
- [NFSEG CLI reference](https://github.com/biometric-technologies/nist-biometric-image-software-nbis/blob/master/man/man1/nfseg.1)
- [NFIQ CLI reference](https://github.com/biometric-technologies/nist-biometric-image-software-nbis/blob/master/man/man1/nfiq.1)
