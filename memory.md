# Memory — Job Watch JSearch Publisher Tracking

Last updated: 2026-06-24

## What was built

Added JSearch publisher tracking to Job Watch. Modified `backend/main.py` to add a nullable `publisher` column migration/index, normalize JSearch `job_publisher` into `publisher`, preserve publisher on insert/update through `upsert_job()`, and expose it through existing row serialization. Modified `frontend/src/main.jsx` so job cards render labels like `JSEARCH · via Indeed`. Updated `backend/test_job_logic.py` with publisher normalization and dedupe/publisher preservation tests.

## Decisions made

`source` remains `jsearch`; `publisher` is a separate underlying-board field. Existing scoring logic and existing scraper functions were not touched. Existing JSearch rows were backfilled from stored apply URL domains rather than reworking source history.

## Problems solved

Existing JSearch cards only showed generic `JSEARCH`. Live schema migration added `publisher`, existing 8 JSearch rows were backfilled, and future JSearch API responses now capture `job_publisher` directly.

## Current state

Live app on `10.10.10.237:8085` is rebuilt and healthy with 8 JSearch rows showing publisher values. Unit tests pass (`20 tests OK`). Docker build/deploy succeeded. Dedupe spot checks found no exact identity duplicates, no canonical URL duplicates, and no JSearch title/company matches against other sources. Working tree has uncommitted changes in `.env.example`, `backend/main.py`, `backend/test_job_logic.py`, `frontend/src/main.jsx`, plus untracked `docker-compose.preserve.yml`.

## Next session starts with

Review the diff, decide whether to commit/push, and if committing include the publisher tracking work plus the existing JSearch integration changes. Do not push without Michael approval.

## Open questions

Whether to keep `docker-compose.preserve.yml` in the repo or treat it as a local deployment helper.
