# Catalog scanning and artwork variants

## Scan admission

Watcher-targeted scans classify and remove only the selected top-level roots. A movie root needs a supported video file. A TV root needs at least one mapped episode. Empty movie, series, and season directories are not persisted.

If a candidate media path or traversal cannot be inspected, defer the whole affected root. Do not reject it or include it in missing-path or move cleanup. Watchdog events or the periodic full scan can retry later.

## Reconcile and watcher behavior

Target comparisons and root locks use platform-aware path normalization. A stale reconcile with no due targets completes without scanning or unscoped cleanup. Persist the first target immediately, batch repeated event counters and revisions, and flush before selection and shutdown.

Windows watcher registration is owned by `orchestrator/app/filesystem_watcher.py`. Its native `ReadDirectoryChangesW` mask excludes last-access notifications so scanner reads do not trigger another reconcile. Do not change the machine-wide Windows last-access policy or patch the installed Watchdog package.

## Local metadata and artwork

NFO sidecars are bounded `metadata` rows for supported movie, series, season, episode, artist, release, and track entities. Normalized local fields take precedence over provider values. Conventional artwork selection must remain consistent across scanning, projections, and image routes.

Artwork variants use the database-adjacent `artwork-variant-cache`, with source-versioned 160 px and 320 px WebP files. Prewarming is bounded background work. There is intentionally no byte-cap or LRU eviction; cleanup removes incomplete temporary files and variants for sources no longer selected. The administrator status endpoint is read-only and returns the in-memory snapshot without conversions or a recursive cache scan.

## Source and verification

Start with `orchestrator/app/catalog.py`, `filesystem_watcher.py`, `artwork_variants.py`, the targeted reconcile services, and their tests under `orchestrator/test/`. Keep behavior changes scoped to the affected root or cache lifecycle and cover deferral and cleanup boundaries.
