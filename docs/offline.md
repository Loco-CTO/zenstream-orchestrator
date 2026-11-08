# Offline access

## Ownership

`offline-v1` routes live in `orchestrator/api/zenstream/offline_routes.py`. Account reconciliation and download selection, preparation, delivery, and metadata reconciliation are split across `orchestrator/app/account_sync.py` and the `offline_*.py` services.

Keep selection and preparation persistent and bounded. Apply account grants before returning catalog records or offline media assets. The Android application owns private device storage, transfer execution, local playback resolution, and offline UI; the Orchestrator owns authorized server snapshots and delivery.

## Download maintenance

The `item-bundle-refresh` capability advertises `GET /api/offline/items/{item_id}/bundle`. It accepts the saved source ID and revision, authorizes current library access, and discovers current external text subtitles and lyrics independently of prepared-asset reservations. Subtitle descriptors use stable file identities and content/display revisions; delivery validates source membership and the requested revision. Prepared asset bundles retain their existing contract.

The item bundle is an external-subtitle snapshot, not a replacement manifest for embedded tracks. When the saved media source changes or disappears, clients retain saved timing-dependent attachments until media replacement. Presentation metadata can still refresh. Available source descriptors contain public media properties only, never filesystem locations.

Android maintains attachments in bounded background work. Full media replacement is explicit by default, optionally automatic at the same source and quality, and uses the normal selection/preparation/delivery routes. Release server support before clients consume the new capability.

## Change and verification route

When changing offline request/response contracts, refresh the curated OpenAPI snapshot and fixtures. Validate Orchestrator offline changes with:

```powershell
$env:PYTHONPATH = 'orchestrator'
python -m unittest discover -s orchestrator/test -p '*_test.py'
python orchestrator/scripts/export_openapi.py --check
python orchestrator/scripts/generate_contract_fixtures.py --check
```

Also update the Android client fixture and offline tests when its consumed behavior changes.
