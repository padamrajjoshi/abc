# Delivery validation — 11 October 2026

## Current results

- Docker image built successfully using Python 3.11 / Debian Bullseye.
- Every build stage uses Debian's signed HTTPS archive repositories, resolving
  package-download 404s after Bullseye's repository transition. Release-file
  expiry checks are disabled only for the frozen archive sources; package
  signature verification remains enabled.
- NBIS scripts' executable permissions were restored from their shebang headers;
  CMake was installed for bundled OpenJPEG; `nfseg` and `nfiq` compiled and were
  installed with their runtime data.
- Official SourceAFIS 3.18.1 and its dependencies resolved successfully via Maven.
- All 11 functional tests passed inside the Docker runtime in 42.97 seconds.
  Coverage includes the three capture modes, anatomical ordering, missing-finger
  rejection, transactional quality rejection, ten-print updates, SQLite restart
  persistence, synthetic comparisons, API workflow, authentication/input limits,
  and actual SourceAFIS enrollment/verification with NBIS quality scoring.
- The initial SourceAFIS test exposed too few synthetic minutiae. Increasing
  procedural ridge dislocations resolved it; the complete suite passed afterward.
- The final image was rebuilt with this tested generator change.
- Service startup and HTTP `/api/health` succeeded on localhost:8001. Port 8000 was
  already occupied, so compose now supports `STUDIO_PORT` (default 8000).
- Health confirmed synthetic acquisition, official SourceAFIS matching and
  installed NFIQ/NFSEG binaries. Engine availability does not guarantee every
  input will use the primary CLI successfully; per-finger output identifies
  actual segmentation/quality engines and any fallback.
- One upstream deprecation warning was emitted by FastAPI/Starlette's HTTPX
  TestClient integration. No tests failed or skipped.

Earlier syntax/configuration checks also passed: Python compilation, dashboard
JavaScript syntax, referenced HTML element IDs, Maven XML, shell script syntax,
and default/hardware compose configurations.

## Remaining limits

- No MORPHS SDK headers/library or physical scanner were supplied. Vendor calls
  and hardware operation cannot be validated; only the Studio adapter interface
  and safe simulation fallback are implemented.
- NFSEG may reject synthetic input and fall back to OpenCV. The software reports
  this and does not describe fallback segmentation as NBIS output.
- No biometric corpus performance test, FBI Appendix F certification or ISO/IEC
  19794-4 interchange validation was performed. Synthetic workflow tests do not
  establish biometric accuracy or production qualification.
- The host Python installation was not changed; dependencies were installed only
  in the authorized Docker image build.
