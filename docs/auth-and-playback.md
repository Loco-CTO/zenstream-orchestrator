# Accounts and playback access

## Client session contract

Client authentication is implemented in `orchestrator/api/zenstream/client_routes.py` and `orchestrator/app/models/account.py`. Clients that opt into `refresh-v1` use short-lived access tokens with rotating refresh tokens, an idle lifetime, and an absolute session cap. Legacy sessions remain valid and upgrade on authenticated bootstrap; do not introduce a forced logout during migration.

Keep session behavior consistent across Web and Android: one refresh attempt after an access-token 401, retry the protected request once, clear local state only for definitive refresh 401/403 responses, and retain it for network or server failures. Cookie and encrypted mobile refresh-token storage remain client-specific.

## Playback capabilities

Playback access is separate from account, resource, and artwork access. Signed playback tickets or `lease-v1` leases are short-lived media capabilities bound to the authorized session and playback target. A lease stores only a hash and is bound to the auth session, entity, source, and optional transcode session.

When a client and server negotiate leases, renew the active lease without changing the media URL's access value or preparing a replacement source. Keep signed-ticket renewal as the fallback when the server does not advertise lease support. Clients that do not opt in retain the legacy ticket contract.

Apply grants before returning catalog, playback, offline, artwork, or resource data. Do not reuse one capability class to authorize another.

## Change and verification route

Start with `orchestrator/app/client_auth.py`, `orchestrator/app/playback.py`, the lease and session models, and `client_routes.py`. If changing public request or response behavior, refresh the curated OpenAPI snapshot and fixtures using `contracts/README.md`, then run the relevant server and client fixture checks.
