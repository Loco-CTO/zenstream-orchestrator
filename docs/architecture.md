# Orchestrator architecture

## Scope and ownership

The Orchestrator is the server boundary for ZenStream. It owns authenticated APIs, account and grant checks, catalog scanning and read models, metadata providers, SQLite persistence, playback negotiation, SyncPlay, calendar integration, jobs, and the optional in-process Lumi host. Web and Android clients consume its APIs; the administrator dashboard and Windows launcher are separate subprojects in this repository.

## Request and service flow

FastAPI route declarations live in `orchestrator/api/`. Routes validate the request and connect it to behavior in `orchestrator/app/`. `orchestrator/app/app.py` composes application startup, middleware, lifespan, routers, and static mounts; it is not a domain-service module.

Domain behavior and long-running work belong in `orchestrator/app/`. Interactive request work uses the appropriate foreground boundary. Jobs and worker tasks own scheduled or background work. `orchestrator/app/models/` defines persistence models, and `migrations/versions/` evolves the database schema.

## API contract flow

The curated OpenAPI contract is assembled by `orchestrator/api/zenstream/openapi.py`. `contracts/openapi.json` and the synthetic fixtures are generated from the source handlers and schemas; `contracts/README.md` documents refresh order and checks. Keep exact compatibility exceptions in `contracts/breaking-change-exceptions.md`.

The Web and Android repositories maintain their client models and consume the shared fixtures through `ZENSTREAM_API_FIXTURE_ROOT`. Cross-repository changes should preserve the server contract and client rollout order.

## Administrator UI and Windows host

- `frontend/` is the separately authenticated administrator dashboard. Its production routes live under `frontend/app/web/`; keep the `/web/*` path contract stable.
- `launcher/` is the Windows x64 Electron host. It starts and supervises one native Orchestrator process; it does not own catalog or account behavior.

## Repository map

- `orchestrator/api/` — route boundaries and OpenAPI documentation assembly.
- `orchestrator/app/` — domain services, workers, adapters, and application composition.
- `migrations/` — database schema history.
- `contracts/` — generated public API snapshots and fixtures.
- `frontend/` — administrator web application.
- `launcher/` — Windows desktop host.
- `docs/` — architecture and behavior guidance.

## Change guidance

Read the focused guide for scanner, calendar, account, playback, or offline changes. Refresh generated API contracts through the documented scripts, then run the relevant backend, frontend, or launcher checks for the changed owner.
