# Agent guidance

## Scope

This repository owns the FastAPI Orchestrator, SQLite persistence, catalog scanning, metadata and account services, administrator dashboard, and Windows launcher. Keep each concern in its owning layer; Web and Android are separate client repositories, and Lumi remains a separately released package.

## Repository map

- `orchestrator/api/` — FastAPI route declarations and curated OpenAPI assembly.
- `orchestrator/app/` — service behavior, catalog and account logic, playback, jobs, and database access.
- `orchestrator/app/models/` and `migrations/` — persisted models and Alembic schema changes.
- `orchestrator/test/` and `tests/` — backend and repository-level tests.
- `contracts/` — generated OpenAPI snapshot, client fixtures, and exact breaking-change exceptions.
- `frontend/app/web/` — separately authenticated administrator dashboard routes.
- `launcher/` — Windows Electron host and its tests.
- `docs/` — architecture and focused service behavior guides.

## Important boundaries

- Keep route declarations in `orchestrator/api/`; `orchestrator/app/app.py` composes middleware, lifespan, routers, and mounts.
- The curated API contract is assembled in `orchestrator/api/zenstream/openapi.py`. After adding or changing client APIs, use the exporter and fixture generator in `orchestrator/scripts/`; never hand-edit generated snapshots or fixtures. See `contracts/README.md`.
- Keep admin dashboard work in `frontend/` and Windows host work in `launcher/`. Dashboard route components own production markup and must preserve the static `/web/*` route contract.
- Read `docs/catalog-scanning.md`, `docs/calendar.md`, or `docs/auth-and-playback.md` when changing those domains.
- Do not edit the installed virtualenv Watchdog package or machine-wide Windows last-access settings. The Windows watcher belongs to `orchestrator/app/filesystem_watcher.py`.
- Before interface changes, read `docs/ui-design.md` and inspect the closest existing screen. Reuse its visual system and navigation patterns, and review the changed flow against that reference.

## Commands

- `python -m unittest discover -s orchestrator/test -p '*_test.py'` — Orchestrator tests.
- `python -m unittest discover -s tests -p 'test*.py'` — repository-level Python tests.
- `python orchestrator/scripts/export_openapi.py --check` — check OpenAPI freshness.
- `python orchestrator/scripts/generate_contract_fixtures.py --check` — check fixture freshness.
- From `launcher/`: `npm ci` and `npm test` — launcher tests.

## Documentation conventions

- Use the same README structure across all four repositories: `How it fits together`, `Configuration`, `Quick start`, `Deployment`, `Development checks`, `Developer documentation`, `Troubleshooting`, `Releases`, and `License`, in that order where relevant. Keep a project title and one-sentence summary at the top; place optional screenshots after the overview and first-run or platform-specific steps under the relevant section.
- Use sentence-case headings, short paragraphs, descriptive links, and repository-relative paths.
- Architecture documents explain ownership and flow. Keep scanner, account/playback, calendar, and offline details in their focused guides rather than in this file.
- Update `docs/ui-design.md` when shared visual or navigation patterns change; keep feature-specific behavior in its focused guide.

## Contribution guidance

- Follow `CONTRIBUTING.md` for issue reports, PR descriptions, validation evidence, and cross-repository coordination.
- Issue forms and the PR template live in `.github/`; label meanings and the dry-run metadata sync command are documented in `docs/triage.md`.
- Keep shared label semantics consistent across the four repositories and tailor area labels and environment prompts to this repository's ownership.
