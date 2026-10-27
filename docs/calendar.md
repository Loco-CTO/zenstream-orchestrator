# Calendar and scheduled jobs

## Ownership

Calendar integration is owned by `orchestrator/app/calendar.py`, `orchestrator/app/models/calendar.py`, and its migrations. Client routes are in `orchestrator/api/zenstream/calendar_routes.py`; administrator provider settings are separate from the grant-filtered user calendar response.

## Provider and event rules

- Sonarr and Radarr connections are mapped to libraries, and API keys are encrypted. Select Sonarr v3 or v5 explicitly; a sync calls only that version. Radarr remains on v3.
- Sonarr episodes need a usable episode or series TVDB identity. Treat a non-empty response without usable identities as an error so existing events remain available.
- Normalize provider dates, timestamps, and IDs. Calculate public `hasFile` per user from grant-visible playable catalog media; do not trust provider flags for authorization.
- After a successful library scan or watcher reconcile, relink that library's stored events before the scan becomes ready. A relink failure is logged and must not fail catalog admission.
- Keep future metadata in `future-metadata-cache` without artwork. Calendar sync may fetch missing or stale metadata for newly discovered unlinked events; the scheduled future-metadata job remains the full retry, refresh, and expiry-cleanup path.

## Schedules and progress

`calendar_sync` and `calendar_future_metadata` are independent daily jobs with server-local default triggers at 03:00 and 04:00. The provider window is UTC and Sunday-aligned, spanning one complete week before the current week through the end of week 16 after it. Future-metadata targeting uses the same inclusive bounds. Both paths report per-identity progress through the standard job-run progress fields.

## Change route

When changing event shape or settings, update the calendar models, migrations, admin and user route contracts, and relevant fixtures. Preserve provider-version selection, grants, and existing-event retention on provider errors.
