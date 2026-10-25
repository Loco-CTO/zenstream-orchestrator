# Offline downloads and account synchronization

The additive authenticated `offline-v1` API is discovered through
`GET /api/offline/capabilities`. Clients bind retained profiles and pending
operations to its durable server-instance UUID, rather than the server URL.
Restoring the database preserves this UUID; creating a new database replaces it.

Selections enumerate in bounded background batches and freeze their membership
before transfer. Clients page at most 100 leaves and explicitly request assets
for their selected exact source, audio track, subtitles and quality. A subsequent
catalog or playlist change never adds downloads automatically.

Originals are hashed in background and delivered without a second permanent
copy. Prepared MP4/H.264/AAC video and M4A/AAC audio require working FFmpeg and
FFprobe tools. Capabilities report only available profiles. HDR conversion also
requires zscale and tonemap filters. Preparation uses one worker per process;
deploy one application process against a database. The worker does not use live
playback sessions. Cancellation is cooperative and restart resumes queued work.

`OFFLINE_PREPARED_MAX_BYTES` defaults to 20 GiB (clamped to 1 GiB–1 TiB).
Preparation additionally preserves 1 GiB free disk space. Temporary files and
prepared media live in the database-adjacent `offline-prepared-cache`.
Reservations last 48 hours and are renewed by repeating the asset request.
Cleanup retains unreferenced terminal assets for seven days and scans at most
100 potential orphan files per pass. Do not delete active prepared files manually.

Delivery supports authenticated GET/HEAD, a single byte range and strong SHA-256
ETags. Clients must send If-Range when resuming, handle a full 200 by restarting,
verify size and digest, and publish local files atomically. A representation
change must never append bytes to an older download. Grant revocation applies
to every remote request; completed app-private files have no periodic expiry.

Bundles include parent metadata, authenticated artwork descriptors, available
selected subtitles, lyrics, chapters and segments. Optional enrichment failures
are reported as warnings. Track manifests describe output indexes. Clients save
supplements locally before claiming their availability and never persist access
tickets in artwork metadata.

Video manifest entries retain `colorTransfer`, `colorPrimaries`, and `isHdr`.
Clients must exclude recognized HDR video from software decoder recovery until
that decoder path implements tone mapping; an independent audio recovery remains
available. Prepared SDR output reports its own color metadata.

Reconciliation accepts up to 100 known catalog objects or one 100-entry playlist
page per request. Playlist page offsets are multiples of 100. Restart a playlist
snapshot when its revision changes between pages. Pending optimistic account
operations must be reapplied over canonical state. Media-source changes are
reported separately and do not require deleting a retained local representation.
Library-access outcomes allow a client to lock known revoked grants while
retaining files when an entity is missing or a server is unreachable.

All new account writes use /api/account/sync, including writes while online.
Keep one durable contiguous sequence per account/device, immutable in-flight
operations, and unique operation UUIDs. The server retains the processed device
watermark independently of transient receipts. Remove local work only after a
terminal acknowledgement. Checkpoints, completion and explicit watched actions
are coherent playback states; music starts accumulate separately. History and
privacy generations prevent stale automatic events restoring cleared history.
Explicit watched actions retain their existing privacy exemption.

Trusted original activity times order competing edits independently of upload
order, with operation UUIDs as deterministic ties. Unreliable clocks may update
only an unchanged base revision. Clients should anchor wall time to server time
and monotonic elapsed time, marking timestamps untrusted after reboot or an
unreliable anchor. Playlist fields, per-track membership and complete ordering
snapshots reconcile separately; deletion tombstones prevent delayed resurrection.
Public sharing remains online-only.

Validation uses isolated SQLite databases and generated synthetic media:
`python -m unittest test.offline_test test.offline_sync_test
test.offline_playlists_test test.offline_selections_test
test.offline_asset_queue_test test.offline_assets_test
test.offline_conversion_test test.offline_audio_conversion_test
test.offline_bundle_test test.offline_reconcile_test` from `orchestrator/`.
Run the OpenAPI exporter and fixture generator with `--check` at repository root.
Rendered synthetic frames and decoded audio are checked on the host running the
tests; Windows results alone do not establish Linux or physical-device acceptance.
