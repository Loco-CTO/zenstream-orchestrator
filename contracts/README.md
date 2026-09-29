# API contracts

The Orchestrator's curated OpenAPI document is exported to `openapi.json`. The
HTTP fixtures are indexed by its stable `operationId` values and include every
non-admin `/api` operation. This intentionally gives the Web and Android
clients a superset of their consumed operations while keeping Orchestrator-only
admin routes in the OpenAPI breaking-change gate.

Fixtures contain deterministic synthetic values. Credential-shaped values are
`<redacted>`, user identifiers are fixture IDs, URLs use `example.invalid`, and
media paths are relative fixture names. The fixtures must not contain real
credentials, account data, or absolute media paths.

To refresh contracts after an API change, run from the Orchestrator repository
root:

```powershell
python orchestrator/scripts/export_openapi.py
python orchestrator/scripts/generate_contract_fixtures.py --write
python orchestrator/scripts/export_openapi.py --check
python orchestrator/scripts/generate_contract_fixtures.py --check
```

The fixture checker validates operation IDs, method/path/parameter/body
references, response status/content, and request and response payloads against
the OpenAPI schemas. `syncplay.json` covers the `/api/ws/syncplay` channel,
which is outside the HTTP OpenAPI paths.

The Orchestrator pull request gate compares the refreshed OpenAPI document to
the base snapshot with `oasdiff breaking --fail-on WARN`, which blocks definite
and potential breaking changes. No exceptions are configured. Any future
exception must be tied to an exact HTTP method and path, documented with its
reason in `contracts/breaking-change-exceptions.md`, and reviewed with the
Orchestrator API change.

The Web and Android repositories keep their client models hand-maintained.
Their focused tests load these shared fixtures through
`ZENSTREAM_API_FIXTURE_ROOT`; CI checks out this directory from the
Orchestrator `main` branch, and the Orchestrator pull request gate runs those
tests against the pull request's fixture directory. The first Orchestrator
pull request establishes the snapshot baseline, so its cross-client jobs are
skipped until that snapshot exists on `main`. Each consumer gate activates once
its fixture test has landed on that repository's `main`, allowing the changes
to roll out in the documented order.
