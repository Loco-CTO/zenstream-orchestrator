# Offline access

## Ownership

`offline-v1` routes live in `orchestrator/api/zenstream/offline_routes.py`. Account reconciliation and download selection, preparation, delivery, and metadata reconciliation are split across `orchestrator/app/account_sync.py` and the `offline_*.py` services.

Keep selection and preparation persistent and bounded. Apply account grants before returning catalog records or offline media assets. The Android application owns private device storage, transfer execution, local playback resolution, and offline UI; the Orchestrator owns authorized server snapshots and delivery.

## Change and verification route

When changing offline request/response contracts, refresh the curated OpenAPI snapshot and fixtures. Validate Orchestrator offline changes with:

```powershell
$env:PYTHONPATH = 'orchestrator'
python -m unittest discover -s orchestrator/test -p '*_test.py'
python orchestrator/scripts/export_openapi.py --check
python orchestrator/scripts/generate_contract_fixtures.py --check
```

Also update the Android client fixture and offline tests when its consumed behavior changes.
