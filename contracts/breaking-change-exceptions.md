# Breaking change exceptions

There are no approved breaking-change exceptions.

The CI gate currently fails on all `ERR` and `WARN` findings. If an exception
is approved with an API change, document the exact HTTP method and path, the
oasdiff finding, the reason it is safe, and the removal condition here. Keep
the ignore entry path-specific and configure the matching `--err-ignore` or
`--warn-ignore` input in the contract workflow. Broad component or wildcard
ignores are not allowed.
