#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/release-candidate.sh"
test_root="$(mktemp -d)"
trap 'rm -rf "$test_root"' EXIT
origin="$test_root/origin.git"
client="$test_root/client"
git init --bare "$origin" >/dev/null
git init --initial-branch=main "$client" >/dev/null
git -C "$client" config user.name "Release workflow test"
git -C "$client" config user.email "release-workflow-test@example.invalid"
git -C "$client" remote add origin "$origin"

cd "$client"
printf 'base\n' > source.txt
git add source.txt
git commit -m "chore: test release base" >/dev/null
base_sha="$(git rev-parse HEAD)"
git push origin "$base_sha:refs/heads/main" "$base_sha:refs/heads/stable" >/dev/null

printf 'candidate\n' > source.txt
git add source.txt
git commit -m "chore: release v1.2.3" -m "release-base-main: $base_sha" -m "release-base-stable: $base_sha" >/dev/null
candidate_sha="$(git rev-parse HEAD)"
candidate_ref="refs/heads/release-candidate/v1.2.3"
tag_ref="refs/tags/v1.2.3"
git push origin "$candidate_sha:$candidate_ref" >/dev/null

test "$(resolve_retry_candidate "$candidate_ref" "$tag_ref" false)" = "$candidate_sha"
git tag v1.2.3 "$candidate_sha"
git push origin "$tag_ref" >/dev/null
test "$(resolve_retry_candidate "$candidate_ref" "$tag_ref" false)" = "$candidate_sha"

mismatch_sha="$(printf 'mismatch\n' | git commit-tree "$(git rev-parse "$base_sha^{tree}")" -p "$base_sha")"
git tag -f v1.2.3 "$mismatch_sha" >/dev/null
git push --force origin "$tag_ref" >/dev/null
if resolve_retry_candidate "$candidate_ref" "$tag_ref" true >/dev/null 2>&1; then
	echo "A candidate/tag SHA mismatch was accepted." >&2
	exit 1
fi
git tag -f v1.2.3 "$candidate_sha" >/dev/null
git push --force origin "$tag_ref" >/dev/null

git push origin ":$candidate_ref" >/dev/null
if resolve_retry_candidate "$candidate_ref" "$tag_ref" false >/dev/null 2>&1; then
	echo "An unpublished tag was accepted without its candidate ref." >&2
	exit 1
fi
test "$(resolve_retry_candidate "$candidate_ref" "$tag_ref" true)" = "$candidate_sha"
git push origin "$candidate_sha:$candidate_ref" >/dev/null
assert_candidate_ref_state "$candidate_ref" "$candidate_sha" true
git push --force origin "$base_sha:$candidate_ref" >/dev/null
if assert_candidate_ref_state "$candidate_ref" "$candidate_sha" true; then
	echo "A moved candidate ref was accepted for publication." >&2
	exit 1
fi
if assert_candidate_ref_state "$candidate_ref" "$candidate_sha" false; then
	echo "An unexpected candidate ref was accepted for a published-tag retry." >&2
	exit 1
fi
git push --force origin "$candidate_sha:$candidate_ref" >/dev/null
assert_candidate_ref_state "$candidate_ref" "$candidate_sha" true
git push origin ":$candidate_ref" >/dev/null
assert_candidate_ref_state "$candidate_ref" "$candidate_sha" false
git push origin "$candidate_sha:$candidate_ref" >/dev/null

git checkout -b drift "$base_sha" >/dev/null
printf 'drift\n' > drift.txt
git add drift.txt
git commit -m "chore: advance main during publish" >/dev/null
drift_sha="$(git rev-parse HEAD)"
git push --force origin "$drift_sha:refs/heads/main" >/dev/null
if promote_ref refs/heads/main "$base_sha" "$candidate_sha" >/dev/null 2>&1; then
	echo "A newer main tip was overwritten." >&2
	exit 1
fi
test "$(remote_ref_sha refs/heads/main)" = "$drift_sha"

git push --force origin "$base_sha:refs/heads/main" >/dev/null
git push --force origin "$drift_sha:refs/heads/stable" >/dev/null
if promote_refs "$candidate_sha" refs/heads/main "$base_sha" refs/heads/stable "$base_sha" >/dev/null 2>&1; then
	echo "Atomic promotion accepted a stable-branch drift." >&2
	exit 1
fi
test "$(remote_ref_sha refs/heads/main)" = "$base_sha"
test "$(remote_ref_sha refs/heads/stable)" = "$drift_sha"
git push --force origin "$base_sha:refs/heads/stable" >/dev/null
promote_refs "$candidate_sha" refs/heads/main "$base_sha" refs/heads/stable "$base_sha" >/dev/null
promote_refs "$candidate_sha" refs/heads/main "$base_sha" refs/heads/stable "$base_sha" >/dev/null
test "$(remote_ref_sha refs/heads/main)" = "$candidate_sha"
test "$(remote_ref_sha refs/heads/stable)" = "$candidate_sha"
delete_candidate_ref "$candidate_ref" "$candidate_sha" >/dev/null
test -z "$(remote_ref_sha "$candidate_ref")"

echo "Release candidate reuse, tag matching, unpublished fallback, branch drift, atomic CAS promotion, and cleanup checks passed."
