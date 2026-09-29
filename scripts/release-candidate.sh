#!/usr/bin/env bash
set -euo pipefail

remote_ref_sha() {
	local ref="$1"
	git ls-remote --heads origin "$ref" |
		awk -v ref="$ref" '$2 == ref { print $1; found = 1; exit } END { if (!found) print "" }'
}

remote_tag_commit_sha() {
	local ref="$1"
	local rows peeled direct
	rows="$(git ls-remote --tags origin "$ref" "${ref}^{}")"
	peeled="$(awk -v ref="${ref}^{}" '$2 == ref { print $1; found = 1; exit } END { if (!found) print "" }' <<<"$rows")"
	if [[ -n "$peeled" ]]; then
		printf '%s\n' "$peeled"
		return
	fi
	direct="$(awk -v ref="$ref" '$2 == ref { print $1; found = 1; exit } END { if (!found) print "" }' <<<"$rows")"
	printf '%s\n' "$direct"
}

resolve_retry_candidate() {
	local candidate_ref="$1"
	local tag_ref="$2"
	local release_published="$3"
	local candidate_sha tag_sha
	candidate_sha="$(remote_ref_sha "$candidate_ref")"
	tag_sha="$(remote_tag_commit_sha "$tag_ref")"

	if [[ -n "$candidate_sha" && -n "$tag_sha" && "$candidate_sha" != "$tag_sha" ]]; then
		echo "Candidate ref $candidate_ref points to $candidate_sha but $tag_ref points to $tag_sha." >&2
		return 1
	fi
	if [[ -n "$candidate_sha" ]]; then
		printf '%s\n' "$candidate_sha"
		return
	fi
	if [[ -n "$tag_sha" && "$release_published" == "true" ]]; then
		printf '%s\n' "$tag_sha"
		return
	fi
	if [[ -n "$tag_sha" ]]; then
		echo "Candidate ref $candidate_ref is missing and $tag_ref belongs to an unpublished release." >&2
	else
		echo "Neither candidate ref $candidate_ref nor release tag $tag_ref exists." >&2
	fi
	return 1
}

release_trailer() {
	local candidate_sha="$1"
	local name="$2"
	git show -s --format=%B "$candidate_sha" |
		awk -F': ' -v key="release-base-$name" '$1 == key { value = $2 } END { print value }'
}

assert_ref_state() {
	local ref="$1"
	local expected_sha="$2"
	local candidate_sha="$3"
	local actual_sha
	actual_sha="$(remote_ref_sha "$ref")"
	if [[ "$actual_sha" != "$expected_sha" && "$actual_sha" != "$candidate_sha" ]]; then
		echo "Ref $ref moved from $expected_sha to ${actual_sha:-<missing>}; refusing release promotion." >&2
		return 1
	fi
}

assert_candidate_ref_state() {
	local ref="$1"
	local candidate_sha="$2"
	local expected_present="$3"
	local actual_sha
	if [[ "$expected_present" != true && "$expected_present" != false ]]; then
		echo "Expected candidate-ref presence must be true or false." >&2
		return 2
	fi
	actual_sha="$(remote_ref_sha "$ref")"
	if [[ "$expected_present" == true && "$actual_sha" != "$candidate_sha" ]]; then
		echo "Candidate ref $ref moved from $candidate_sha to ${actual_sha:-<missing>}." >&2
		return 1
	fi
	if [[ "$expected_present" == false && -n "$actual_sha" && "$actual_sha" != "$candidate_sha" ]]; then
		echo "Candidate ref $ref now points to $actual_sha instead of $candidate_sha." >&2
		return 1
	fi
}

promote_ref() {
	local ref="$1"
	local expected_sha="$2"
	local candidate_sha="$3"
	local actual_sha
	actual_sha="$(remote_ref_sha "$ref")"
	if [[ "$actual_sha" == "$candidate_sha" ]]; then
		echo "Ref $ref already points to candidate $candidate_sha."
		return
	fi
	if [[ "$actual_sha" != "$expected_sha" ]]; then
		echo "Ref $ref moved from $expected_sha to ${actual_sha:-<missing>}; refusing to overwrite it." >&2
		return 1
	fi
	if ! git merge-base --is-ancestor "$expected_sha" "$candidate_sha"; then
		echo "Candidate $candidate_sha is not a fast-forward from $ref at $expected_sha." >&2
		return 1
	fi
	git push --force-with-lease="$ref:$expected_sha" origin "$candidate_sha:$ref"
	actual_sha="$(remote_ref_sha "$ref")"
	if [[ "$actual_sha" != "$candidate_sha" ]]; then
		echo "Ref $ref did not settle at candidate $candidate_sha after promotion." >&2
		return 1
	fi
}

promote_refs() {
	local candidate_sha="$1"
	shift
	if (( $# == 0 || $# % 2 != 0 )); then
		echo "promote_refs expects ref/base pairs after the candidate SHA." >&2
		return 2
	fi

	local -a leases=()
	local -a refspecs=()
	local ref expected actual refspec
	while (( $# > 0 )); do
		ref="$1"
		expected="$2"
		shift 2
		actual="$(remote_ref_sha "$ref")"
		if [[ "$actual" != "$expected" && "$actual" != "$candidate_sha" ]]; then
			echo "Ref $ref moved from $expected to ${actual:-<missing>}; refusing atomic promotion." >&2
			return 1
		fi
		if [[ "$actual" != "$candidate_sha" ]] &&
			! git merge-base --is-ancestor "$expected" "$candidate_sha"; then
			echo "Candidate $candidate_sha is not a fast-forward from $ref at $expected." >&2
			return 1
		fi
		leases+=("--force-with-lease=$ref:$actual")
		refspecs+=("$candidate_sha:$ref")
	done

	local all_candidate=true
	for refspec in "${refspecs[@]}"; do
		ref="${refspec#*:}"
		[[ "$(remote_ref_sha "$ref")" == "$candidate_sha" ]] || all_candidate=false
	done
	if [[ "$all_candidate" == true ]]; then
		echo "All release branches already point to candidate $candidate_sha."
		return
	fi

	git push --atomic "${leases[@]}" origin "${refspecs[@]}"
	for refspec in "${refspecs[@]}"; do
		ref="${refspec#*:}"
		actual="$(remote_ref_sha "$ref")"
		if [[ "$actual" != "$candidate_sha" ]]; then
			echo "Ref $ref did not settle at candidate $candidate_sha after atomic promotion." >&2
			return 1
		fi
	done
}

delete_candidate_ref() {
	local ref="$1"
	local candidate_sha="$2"
	local actual_sha
	actual_sha="$(remote_ref_sha "$ref")"
	if [[ -z "$actual_sha" ]]; then
		echo "Candidate ref $ref is already absent."
		return
	fi
	if [[ "$actual_sha" != "$candidate_sha" ]]; then
		echo "Candidate ref $ref moved to $actual_sha; refusing to delete it." >&2
		return 1
	fi
	git push --force-with-lease="$ref:$candidate_sha" origin ":$ref"
}
