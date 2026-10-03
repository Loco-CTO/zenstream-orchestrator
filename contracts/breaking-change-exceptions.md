# Breaking change exceptions

All ten findings use oasdiff's `request-parameter-enum-value-removed` check and
correct the published contract without removing runtime-accepted inputs. Home
still accepts all five listed values; its enum moved into the string branch of
the nullable `anyOf`, which oasdiff reports as removals from the old top-level
enum. Item detail accepts only `header`, `episodes`, `similar`, and `credits`;
its old Home values were rejected by the handler. The correction documents
existing runtime behavior. Remove these entries after the corrected snapshot
has become the comparison base.

GET /api/catalog/home removed the enum value `continueWatching` from the `query` request parameter `section`
GET /api/catalog/home removed the enum value `derived` from the `query` request parameter `section`
GET /api/catalog/home removed the enum value `featured` from the `query` request parameter `section`
GET /api/catalog/home removed the enum value `library` from the `query` request parameter `section`
GET /api/catalog/home removed the enum value `nextUp` from the `query` request parameter `section`
GET /api/catalog/items/{entity_id}/detail removed the enum value `continueWatching` from the `query` request parameter `section`
GET /api/catalog/items/{entity_id}/detail removed the enum value `derived` from the `query` request parameter `section`
GET /api/catalog/items/{entity_id}/detail removed the enum value `featured` from the `query` request parameter `section`
GET /api/catalog/items/{entity_id}/detail removed the enum value `library` from the `query` request parameter `section`
GET /api/catalog/items/{entity_id}/detail removed the enum value `nextUp` from the `query` request parameter `section`

The CI gate currently fails on all `ERR` and `WARN` findings. If an exception
is approved with an API change, document the exact HTTP method and path, the
oasdiff finding, the reason it is safe, and the removal condition here. Keep
the ignore entry path-specific and configure the matching `--err-ignore` or
`--warn-ignore` input in the contract workflow. Broad component or wildcard
ignores are not allowed.
